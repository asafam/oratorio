#!/usr/bin/env python3
"""The listener: one small process beside each persistent agent session.

It waits on Postgres LISTEN (plain code -- no model call, no tokens) and,
when the board has something for its agent, hands the message to that
agent's already-running session. The session itself is an ordinary
interactive Claude Code / Codex session; this process is its only link to
"something arrived while you were idle".

On start it registers the agent as online (see registry.register), then
keeps a heartbeat going so other agents can see who is up.

NOTIFY is treated as a latency hint only: every (re)connect and a periodic
timer both run a full sweep of the agent's pending deliveries, so a dropped
connection or a missed notification only adds latency. Durability lives in
board.message_delivery (see board_core/messages.py), and holds even while
this whole process is down for hours.

NOT BUILT YET: `hand_to_session` below only prints each message as one
JSON line on stdout. Pushing that text into a live Claude Code / Codex
session (and deciding when the session is idle enough to take it) is the
missing piece -- replace `hand_to_session` when that mechanism is chosen.

Environment:
    ORCH_BOARD_DSN    Postgres DSN. LISTEN needs a real database
                      connection -- the MCP endpoint alone is not enough.
    ORCH_AGENT_TOKEN  this agent's token (from sync_roles.py).
    ORCH_RUNNER       what the session is: claude | codex | ...
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any, Callable

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from orchestration.board_core import auth, db, messages, registry  # noqa: E402
from orchestration.listener import channels  # noqa: E402

logger = logging.getLogger("orchestration.listener")

RECONNECT_DELAY_SECONDS = 5
# Heartbeat + sweep interval. Must stay well under
# registry.ONLINE_WINDOW_SECONDS or the agent flickers offline.
HEARTBEAT_SECONDS = 30

Handler = Callable[[dict[str, Any]], None]


def hand_to_session(item: dict[str, Any]) -> None:
    """STUB -- see the module docstring. One JSON line per item."""
    print(json.dumps(item, default=str), flush=True)


class Listener:
    def __init__(self, agent_id: str, runner: str, handler: Handler = hand_to_session):
        self.agent_id = agent_id
        self.runner = runner
        self.handler = handler
        self._last_thread_id: str | None = None
        # A NOTIFY-triggered sweep and the periodic one run on different
        # worker threads -- without this they could hand over the same message twice.
        self._sweep_lock = threading.Lock()

    def start_session(self) -> None:
        """Announce the agent and put back anything the previous session
        was handed but never acked -- it is not in the new session's
        context, so it has to be handed in again."""
        with db.get_pool().connection() as conn:
            registry.register(conn, self.agent_id, runner=self.runner)
            requeued = messages.requeue_unacked(conn, self.agent_id)
            conn.commit()
        if requeued:
            logger.info("agent=%s re-queued %d unacked message(s)", self.agent_id, requeued)

    def sweep(self) -> None:
        """Hand over everything waiting, then any reply deadlines that have
        passed. A message is marked delivered only after the hand-off
        returned -- if the handler raises, it stays on the work list."""
        with self._sweep_lock, db.get_pool().connection() as conn:
            registry.heartbeat(conn, self.agent_id)
            conn.commit()
            for msg in messages.undelivered(conn, self.agent_id):
                # A different thread than the last message means new work --
                # the cue for suggesting a /clear or /compact first.
                new_thread = msg["thread_id"] is not None and msg["thread_id"] != self._last_thread_id
                self.handler({"kind": "message", "new_thread": new_thread, **msg})
                self._last_thread_id = msg["thread_id"]
                messages.mark_delivered(conn, self.agent_id, [msg["id"]])
                conn.commit()
            for req in messages.claim_overdue_requests(conn, self.agent_id):
                self.handler({"kind": "reply_timeout", **req})
            conn.commit()

    def channels(self) -> set[str]:
        with db.get_pool().connection() as conn:
            return channels.agent_channels(conn, self.agent_id)


async def _periodic_sweep(listener: Listener) -> None:
    """Heartbeat, plus belt-and-braces: a listener that is still connected
    but has, for whatever reason, missed a NOTIFY still self-heals. Also
    what notices a reply deadline passing, since nothing NOTIFYs for that."""
    while True:
        await asyncio.sleep(HEARTBEAT_SECONDS)
        try:
            await asyncio.to_thread(listener.sweep)
        except Exception:
            logger.exception("periodic sweep failed")


async def run(listener: Listener) -> None:
    dsn = os.environ["ORCH_BOARD_DSN"]
    # Once per listener start, NOT per reconnect: re-queueing on every
    # reconnect would hand the session messages it already has.
    while True:
        try:
            await asyncio.to_thread(listener.start_session)
            break
        except (psycopg.OperationalError, OSError) as e:
            logger.warning("board not reachable yet (%s) -- retrying in %ds", e, RECONNECT_DELAY_SECONDS)
            await asyncio.sleep(RECONNECT_DELAY_SECONDS)
    periodic = asyncio.create_task(_periodic_sweep(listener))

    try:
        while True:
            try:
                async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as aconn:
                    listened: set[str] = set()

                    async def ensure_listening() -> None:
                        wanted = await asyncio.to_thread(listener.channels)
                        for ch in wanted - listened:
                            await aconn.execute(f"LISTEN {ch}")
                            listened.add(ch)

                    # LISTEN first, then sweep: anything posted in between
                    # is caught by one or the other, never neither.
                    await ensure_listening()
                    logger.info("agent=%s listening on %d channel(s)", listener.agent_id, len(listened))
                    await asyncio.to_thread(listener.sweep)

                    async for notify in aconn.notifies():
                        if notify.channel == channels.TOPOLOGY_CHANNEL:
                            await ensure_listening()
                            continue
                        await asyncio.to_thread(listener.sweep)
            except (psycopg.OperationalError, OSError) as e:
                logger.warning("listener connection lost (%s) -- reconnecting in %ds (anything "
                               "that arrives during the gap is durable in message_delivery and "
                               "is picked up by the sweep right after reconnect)",
                               e, RECONNECT_DELAY_SECONDS)
                await asyncio.sleep(RECONNECT_DELAY_SECONDS)
    finally:
        periodic.cancel()


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)  # stdout is the hand-off stream
    token = os.environ.get("ORCH_AGENT_TOKEN")
    if not token:
        print("ORCH_AGENT_TOKEN is not set -- refusing to start.", file=sys.stderr)
        sys.exit(1)
    with db.get_pool().connection() as conn:
        try:
            agent_id = auth.resolve_sender(conn, token)
        except auth.AuthError as e:
            print(f"ORCH_AGENT_TOKEN rejected: {e}", file=sys.stderr)
            sys.exit(1)
    asyncio.run(run(Listener(agent_id, os.environ.get("ORCH_RUNNER", "claude"))))


if __name__ == "__main__":
    main()
