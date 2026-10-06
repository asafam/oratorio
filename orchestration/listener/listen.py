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

The session takes one thing at a time: the listener hands over a message
only while the session is idle, then waits for it to go idle again before
the next. With ORCH_TMUX_PANE set, "hand over" means typing it into that
tmux pane (see session/tmux.py); without it, each item is printed as a
JSON line on stdout instead.

Environment:
    ORCH_BOARD_DSN    Postgres DSN. LISTEN needs a real database
                      connection -- the MCP endpoint alone is not enough.
    ORCH_AGENT_TOKEN  this agent's token.
    ORCH_RUNNER       what the session is: claude | codex | ...
    ORCH_TMUX_PANE    tmux pane id (%N) of the agent's session.
    ORCH_IDLE_FLAG    file that exists while that session is idle.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any, Protocol

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from orchestration.board_core import auth, db, messages, registry  # noqa: E402
from orchestration.listener import channels  # noqa: E402
from orchestration.session.tmux import TmuxSession  # noqa: E402

logger = logging.getLogger("orchestration.listener")

RECONNECT_DELAY_SECONDS = 5
# Heartbeat + sweep interval. Must stay well under
# registry.ONLINE_WINDOW_SECONDS or the agent flickers offline.
HEARTBEAT_SECONDS = 30

# How often to look at whether a busy session has gone idle. A local
# check (no database, no model) -- only does anything while work is waiting.
IDLE_CHECK_SECONDS = 1


class Session(Protocol):
    def ready(self) -> bool: ...          # idle and able to take one item now
    def was_cleared(self) -> bool: ...    # context was wiped since last asked
    def hand(self, item: dict[str, Any]) -> None: ...


class StdoutSession:
    """Fallback when there is no pane to type into: one JSON line per item."""

    def ready(self) -> bool:
        return True

    def was_cleared(self) -> bool:
        return False

    def hand(self, item: dict[str, Any]) -> None:
        print(json.dumps(item, default=str), flush=True)


class Listener:
    def __init__(self, agent_id: str, runner: str, session: Session | None = None):
        self.agent_id = agent_id
        self.runner = runner
        self.session = session or StdoutSession()
        self._last_thread_id: str | None = None
        self._timeout_notes: list[dict[str, Any]] = []
        # True while something is waiting for the session to go idle.
        self.backlog = False
        # A NOTIFY-triggered sweep and the timed ones run on different
        # worker threads -- without this they could hand over the same message twice.
        self._sweep_lock = threading.Lock()

    def start_session(self) -> None:
        """Announce the agent and put back anything the previous session
        was handed but never acked -- it is not in the new session's
        context, so it has to be handed in again.

        Not when only the listener was restarted and the session lived on
        (ORCH_SESSION_KEPT is set): what it was handed is still in its
        context, and handing it all in again would only repeat old work."""
        with db.get_pool().connection() as conn:
            registry.register(conn, self.agent_id, runner=self.runner)
            requeued = 0 if os.environ.get("ORCH_SESSION_KEPT") else messages.requeue_unacked(conn, self.agent_id)
            conn.commit()
        if requeued:
            logger.info("agent=%s re-queued %d unacked message(s)", self.agent_id, requeued)

    def sweep(self) -> None:
        """Hand over what is waiting, one item per idle moment: messages
        first, then any reply deadlines that have passed. A message is
        marked delivered only after the hand-off returned -- if that
        raises, it stays on the work list."""
        with self._sweep_lock, db.get_pool().connection() as conn:
            registry.heartbeat(conn, self.agent_id)
            if self.session.was_cleared():
                requeued = messages.requeue_unacked(conn, self.agent_id)
                logger.info("agent=%s session was cleared, re-queued %d open message(s)",
                            self.agent_id, requeued)
            conn.commit()
            self._timeout_notes += messages.claim_overdue_requests(conn, self.agent_id)
            conn.commit()

            while self.session.ready():
                waiting = messages.undelivered(conn, self.agent_id, limit=1)
                if waiting:
                    msg = waiting[0]
                    # A different thread than the last message handed in means
                    # new work -- the cue for suggesting a /clear or /compact
                    # first. Never on the first message: there is nothing to clear.
                    new_thread = (
                        self._last_thread_id is not None
                        and msg["thread_id"] is not None
                        and msg["thread_id"] != self._last_thread_id
                    )
                    self.session.hand({"kind": "message", "new_thread": new_thread, **msg})
                    self._last_thread_id = msg["thread_id"] or self._last_thread_id
                    messages.mark_delivered(conn, self.agent_id, [msg["id"]])
                    conn.commit()
                elif self._timeout_notes:
                    self.session.hand({"kind": "reply_timeout", **self._timeout_notes.pop(0)})
                else:
                    break
            self.backlog = bool(self._timeout_notes) or bool(
                messages.undelivered(conn, self.agent_id, limit=1)
            )
        if self.backlog and isinstance(self.session, TmuxSession):
            self.session.nudge_if_held(self.agent_id)

    def wants_sweep(self) -> bool:
        """Cheap local check: is there a reason to sweep right now, other
        than a NOTIFY or the heartbeat timer?"""
        if isinstance(self.session, TmuxSession) and self.session.cleared_marker.exists():
            return True
        return self.backlog and self.session.ready()

    def channels(self) -> set[str]:
        with db.get_pool().connection() as conn:
            return channels.agent_channels(conn, self.agent_id)


async def _idle_watch(listener: Listener) -> None:
    """Notices the session going idle (or being cleared) while work is
    waiting -- nothing NOTIFYs for that, it is a local file appearing."""
    while True:
        await asyncio.sleep(IDLE_CHECK_SECONDS)
        try:
            if listener.wants_sweep():
                await asyncio.to_thread(listener.sweep)
        except Exception:
            logger.exception("idle-triggered sweep failed")


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
    idle_watch = asyncio.create_task(_idle_watch(listener))

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
        idle_watch.cancel()


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
    session: Session | None = None
    runner = os.environ.get("ORCH_RUNNER", "claude")
    pane, idle_flag = os.environ.get("ORCH_TMUX_PANE"), os.environ.get("ORCH_IDLE_FLAG")
    if pane and idle_flag:
        session = TmuxSession(pane, Path(idle_flag), runner)
    asyncio.run(run(Listener(agent_id, runner, session)))


if __name__ == "__main__":
    main()
