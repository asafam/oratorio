#!/usr/bin/env python3
"""The board view: a live, read-only feed of what the agents are saying to
each other -- who is online, every message as it is posted, and when each
recipient is handed it and finishes it. Leave it running in its own
terminal pane.

This is the operator's tool, not an agent's: it reads the database
directly with ORCH_BOARD_DSN (no agent token) and never writes, so
watching does not create delivery rows or mark anything as seen.

It checks the database once a second. That is plain code against Postgres
-- no model is involved, so it costs no tokens.

With --workspace it shows that one team, by agent name. Without, it shows
every workspace on the board, and agents appear as `workspace/name`.

Usage:
    python -m orchestration.watch.board [--workspace NAME] [--history N] [--once]
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from orchestration.board_core import registry  # noqa: E402

POLL_SECONDS = 1
CONTENT_WIDTH = 100

_COLOR = sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def _clock(ts: datetime) -> str:
    return ts.astimezone().strftime("%H:%M:%S")


def _one_line(content: str) -> str:
    text = " ".join(content.split())
    return text if len(text) <= CONTENT_WIDTH else text[: CONTENT_WIDTH - 1] + "…"


def format_agent(agent: dict[str, Any]) -> str:
    if agent["online"]:
        return _c("32", f"● {agent['label']}") + f"  online ({agent['runner'] or '?'})"
    last = f"last seen {_clock(agent['last_seen_at'])}" if agent["last_seen_at"] else "never seen"
    return _c("2", f"○ {agent['label']}  offline, {last}")


def _label(alias: str, workspace: str | None) -> str:
    """SQL for how an agent is shown: its name inside one workspace, its
    full key when several workspaces share the screen."""
    return f"{alias}.name" if workspace is not None else f"{alias}.agent_id"


def format_message(m: dict[str, Any]) -> str:
    if m["is_broadcast"]:
        target = "everyone"
    elif m["topic"]:
        target = f"#{m['topic']}"
    else:
        target = m["recipient"]
    tags = []
    if m["in_reply_to"]:
        tags.append(f"reply to {m['in_reply_to']}")
    if m["thread_id"]:
        tags.append(f"thread {m['thread_id']}")
    if m["reply_by"]:
        tags.append(f"wants reply by {_clock(m['reply_by'])}")
    if m["expires_at"]:
        tags.append(f"expires {_clock(m['expires_at'])}")
    head = f"{_clock(m['created_at'])}  {_c('1', str(m['id']))}  {m['sender']} -> {target}"
    if tags:
        head += _c("2", "  [" + ", ".join(tags) + "]")
    lines = [head, f"          {_one_line(m['content'])}"]
    if not m["recipients"]:
        lines.append(_c("33", "          (delivered to nobody)"))
    elif m["topic"] or m["is_broadcast"]:
        lines.append(_c("2", "          to: " + ", ".join(m["recipients"])))
    return "\n".join(lines)


def format_event(e: dict[str, Any]) -> str:
    text = {
        "delivered": f"handed to {e['agent_id']}",
        "acked": f"{e['agent_id']} finished it",
        "reply_timeout": f"no reply by the deadline (sender {e['agent_id']} was told)",
    }[e["kind"]]
    line = f"{_clock(e['at'])}  {e['message_id']}  {text}"
    return _c("33", line) if e["kind"] == "reply_timeout" else _c("2", line)


def fetch_agents(conn: psycopg.Connection, workspace: str | None) -> list[dict]:
    agents = registry.list_agents(conn, workspace=workspace)
    for agent in agents:
        agent["label"] = agent["name"] if workspace is not None else agent["agent_id"]
    return agents


def fetch_messages(conn: psycopg.Connection, *, after_id: int, workspace: str | None = None,
                   limit: int | None = None) -> list[dict]:
    """Messages with id > after_id, oldest first. With `limit`, only the
    newest `limit` of them (still returned oldest first)."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            SELECT * FROM (
                SELECT m.id, {_label("s", workspace)} AS sender, {_label("r", workspace)} AS recipient,
                       m.topic, m.is_broadcast, m.content, m.in_reply_to, m.thread_id,
                       m.reply_by, m.expires_at, m.created_at,
                       ARRAY(SELECT {_label("a", workspace)} FROM board.message_delivery d
                             JOIN board.agent a ON a.agent_id = d.agent_id
                             WHERE d.message_id = m.id ORDER BY 1) AS recipients
                FROM board.message m
                JOIN board.agent s ON s.agent_id = m.sender_agent_id
                LEFT JOIN board.agent r ON r.agent_id = m.recipient_agent_id
                WHERE m.id > %(after)s AND (%(ws)s::text IS NULL OR m.workspace = %(ws)s)
                ORDER BY m.id DESC
                LIMIT %(limit)s
            ) newest ORDER BY id
            """,
            {"after": after_id, "ws": workspace, "limit": limit},
        )
        return cur.fetchall()


def fetch_events(conn: psycopg.Connection, *, since: datetime, until: datetime,
                 workspace: str | None = None) -> list[dict]:
    """Hand-overs, acks and reply timeouts stamped in (since, until]."""
    label = _label("a", workspace)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            SELECT 'delivered' AS kind, d.delivered_at AS at, d.message_id, {label} AS agent_id
              FROM board.message_delivery d JOIN board.agent a ON a.agent_id = d.agent_id
             WHERE d.delivered_at > %(since)s AND d.delivered_at <= %(until)s
               AND (%(ws)s::text IS NULL OR a.workspace = %(ws)s)
            UNION ALL
            SELECT 'acked', d.acked_at, d.message_id, {label}
              FROM board.message_delivery d JOIN board.agent a ON a.agent_id = d.agent_id
             WHERE d.acked_at > %(since)s AND d.acked_at <= %(until)s
               AND (%(ws)s::text IS NULL OR a.workspace = %(ws)s)
            UNION ALL
            SELECT 'reply_timeout', m.reply_timeout_noted_at, m.id, {label}
              FROM board.message m JOIN board.agent a ON a.agent_id = m.sender_agent_id
             WHERE m.reply_timeout_noted_at > %(since)s AND m.reply_timeout_noted_at <= %(until)s
               AND (%(ws)s::text IS NULL OR m.workspace = %(ws)s)
            ORDER BY at, message_id
            """,
            {"since": since, "until": until, "ws": workspace},
        )
        return cur.fetchall()


def _db_now(conn: psycopg.Connection) -> datetime:
    return conn.execute("SELECT now()").fetchone()[0]


def watch(conn: psycopg.Connection, *, history: int, once: bool, workspace: str | None = None) -> None:
    presence: dict[str, bool] = {}

    def show_presence_changes() -> None:
        for agent in fetch_agents(conn, workspace):
            if presence.get(agent["agent_id"]) != agent["online"]:
                presence[agent["agent_id"]] = agent["online"]
                print(format_agent(agent))

    show_presence_changes()
    print(_c("2", "-" * 60))

    # Events are followed by the database's own clock, so a difference
    # between this machine's clock and the board's can't skip or repeat one.
    seen_until = _db_now(conn)
    last_id = 0
    for m in fetch_messages(conn, after_id=0, workspace=workspace, limit=history):
        print(format_message(m))
        last_id = m["id"]
    if once:
        return
    if last_id == 0:  # empty history: still start after whatever exists
        last_id = conn.execute("SELECT COALESCE(max(id), 0) FROM board.message").fetchone()[0]

    while True:
        sys.stdout.flush()
        time.sleep(POLL_SECONDS)
        show_presence_changes()
        for m in fetch_messages(conn, after_id=last_id, workspace=workspace):
            print(format_message(m))
            last_id = m["id"]
        now = _db_now(conn)
        for e in fetch_events(conn, since=seen_until, until=now, workspace=workspace):
            print(format_event(e))
        seen_until = now


def main() -> None:
    parser = argparse.ArgumentParser(description="Live feed of the agents' message board.")
    parser.add_argument("-w", "--workspace", help="show only this workspace (default: all)")
    parser.add_argument("--history", type=int, default=20, help="recent messages to show first (default 20)")
    parser.add_argument("--once", action="store_true", help="print the current state and exit")
    args = parser.parse_args()

    dsn = os.environ.get("ORCH_BOARD_DSN")
    if not dsn:
        print("ORCH_BOARD_DSN is not set.", file=sys.stderr)
        sys.exit(1)
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            watch(conn, history=args.history, once=args.once, workspace=args.workspace)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
