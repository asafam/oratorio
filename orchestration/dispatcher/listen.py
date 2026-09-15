#!/usr/bin/env python3
"""The dispatcher: turns board activity into webhook wake-ups.

Runs on the board-host, colocated with Postgres (its own LISTEN connection
is local, never crosses a tunnel -- see orchestration/README.md). NOTIFY is
treated as a latency hint only: every reconnect (including the very first
connect) runs a full reconcile sweep over board.message_delivery for
agents with unread work, and a periodic reconcile runs regardless of
NOTIFY traffic, so a dropped connection or a missed notification only adds
latency, never silently drops a wake-up -- delivery durability itself
already lives in the message_delivery table (see board_core/messages.py);
this module's job is purely "wake the right process soon", not
"guarantee it eventually gets read" (that guarantee holds even if this
whole process is down).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from orchestration.board_core import db, messages, registry  # noqa: E402
from orchestration.dispatcher import channels, deliver  # noqa: E402

logger = logging.getLogger("orchestration.dispatcher")

RECONNECT_DELAY_SECONDS = 5
PERIODIC_RECONCILE_SECONDS = 300
# message_id 0 is a sentinel for "reconcile wake" (not tied to one
# specific message) -- harmless, since the receiver's payload is metadata
# only and it always re-reads via read_messages once woken regardless.
RECONCILE_SENTINEL_MESSAGE_ID = 0


def _sync_wake(agent_id: str, message_id: int, mcp_url: str) -> None:
    """Runs in a worker thread: board_core is sync, and deliver_with_retry
    blocks on its backoff schedule -- never run either on the event loop."""
    with db.get_pool().connection() as conn:
        creds = registry.try_consume_wake_budget(conn, agent_id)
        conn.commit()
    if creds is None:
        logger.info("agent=%s wake skipped (inactive, or over its hourly wake budget)", agent_id)
        return
    if not creds.get("webhook_url") or not creds.get("webhook_secret"):
        logger.warning("agent=%s has no webhook_url/webhook_secret configured yet -- cannot wake", agent_id)
        return
    result = deliver.deliver_with_retry(
        creds["webhook_url"], creds["webhook_secret"], message_id=message_id, mcp_url=mcp_url
    )
    if not result.delivered:
        logger.error(
            "agent=%s wake delivery FAILED after %d attempt(s): %s -- message stays durably "
            "unread in board.message_delivery, next reconcile sweep will retry",
            agent_id, result.attempts, result.error,
        )


def _sync_pending_recipients(message_id: int) -> list[str]:
    with db.get_pool().connection() as conn:
        return messages.pending_recipients(conn, message_id)


def _sync_all_pending_agents() -> set[str]:
    with db.get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT agent_id FROM board.message_delivery WHERE read_at IS NULL"
        ).fetchall()
        return {r[0] for r in rows}


def _sync_active_channels() -> set[str]:
    with db.get_pool().connection() as conn:
        return channels.active_channels(conn)


async def _handle_notify(notify: psycopg.Notify, mcp_url: str) -> None:
    try:
        payload = json.loads(notify.payload) if notify.payload else {}
    except json.JSONDecodeError:
        logger.warning("bad NOTIFY payload on channel=%s: %r", notify.channel, notify.payload)
        return
    message_id = payload.get("message_id")
    if message_id is None:
        return
    recipients = await asyncio.to_thread(_sync_pending_recipients, message_id)
    if not recipients:
        return  # already read by everyone, or depth_remaining hit 0 (no delivery rows at all)
    await asyncio.gather(
        *(asyncio.to_thread(_sync_wake, agent_id, message_id, mcp_url) for agent_id in recipients)
    )


async def _reconcile(mcp_url: str) -> None:
    agents = await asyncio.to_thread(_sync_all_pending_agents)
    if not agents:
        return
    logger.info("reconcile: %d agent(s) have unread messages, waking each", len(agents))
    await asyncio.gather(
        *(asyncio.to_thread(_sync_wake, agent_id, RECONCILE_SENTINEL_MESSAGE_ID, mcp_url) for agent_id in agents)
    )


async def _periodic_reconcile(mcp_url: str) -> None:
    """Belt-and-braces: reconcile runs on every reconnect already, but this
    also runs on a fixed timer regardless of connection state, so a
    dispatcher that's technically still connected but has, for whatever
    reason, missed NOTIFYs still self-heals."""
    while True:
        await asyncio.sleep(PERIODIC_RECONCILE_SECONDS)
        try:
            await _reconcile(mcp_url)
        except Exception:
            logger.exception("periodic reconcile failed")


async def run(mcp_url: str) -> None:
    dsn = os.environ["ORCH_BOARD_DSN"]
    asyncio.create_task(_periodic_reconcile(mcp_url))

    while True:
        try:
            async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as aconn:
                logger.info("dispatcher connected -- reconciling before listening")
                await _reconcile(mcp_url)

                listened: set[str] = set()

                async def ensure_listening() -> None:
                    wanted = await asyncio.to_thread(_sync_active_channels)
                    for ch in wanted - listened:
                        await aconn.execute(f"LISTEN {ch}")
                        listened.add(ch)

                await ensure_listening()
                logger.info("listening on %d channel(s)", len(listened))

                async for notify in aconn.notifies():
                    if notify.channel == channels.TOPOLOGY_CHANNEL:
                        await ensure_listening()
                        continue
                    await _handle_notify(notify, mcp_url)
        except (psycopg.OperationalError, OSError) as e:
            logger.warning("dispatcher connection lost (%s) -- reconnecting in %ds "
                            "(any messages that arrived during the gap are durable in "
                            "message_delivery and will be picked up by the reconcile sweep "
                            "right after reconnect)", e, RECONNECT_DELAY_SECONDS)
            await asyncio.sleep(RECONNECT_DELAY_SECONDS)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    mcp_url = os.environ.get("ORCH_MCP_URL")
    if not mcp_url:
        print("ORCH_MCP_URL is not set (the board-host's MCP endpoint, passed through to "
              "each wake-up payload) -- refusing to start.", file=sys.stderr)
        sys.exit(1)
    asyncio.run(run(mcp_url))


if __name__ == "__main__":
    main()
