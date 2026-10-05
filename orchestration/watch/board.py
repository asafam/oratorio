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

Usage:
    python -m orchestration.watch.board [--history N] [--once]
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
        return _c("32", f"● {agent['agent_id']}") + f"  online ({agent['runner'] or '?'})"
    last = f"last seen {_clock(agent['last_seen_at'])}" if agent["last_seen_at"] else "never seen"
    return _c("2", f"○ {agent['agent_id']}  offline, {last}")


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


def fetch_messages(conn: psycopg.Connection, *, after_id: int, limit: int | None = None) -> list[dict]:
    """Messages with id > after_id, oldest first. With `limit`, only the
    newest `limit` of them (still returned oldest first)."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT * FROM (
                SELECT m.id, m.sender_agent_id AS sender, m.recipient_agent_id AS recipient,
                       m.topic, m.is_broadcast, m.content, m.in_reply_to, m.thread_id,
                       m.reply_by, m.expires_at, m.created_at,
                       ARRAY(SELECT d.agent_id FROM board.message_delivery d
                             WHERE d.message_id = m.id ORDER BY d.agent_id) AS recipients
                FROM board.message m
                WHERE m.id > %s
                ORDER BY m.id DESC
                LIMIT %s
            ) newest ORDER BY id
            """,
            (after_id, limit),
        )
        return cur.fetchall()


def fetch_events(conn: psycopg.Connection, *, since: datetime, until: datetime) -> list[dict]:
    """Hand-overs, acks and reply timeouts stamped in (since, until]."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT 'delivered' AS kind, delivered_at AS at, message_id, agent_id
              FROM board.message_delivery WHERE delivered_at > %(since)s AND delivered_at <= %(until)s
            UNION ALL
            SELECT 'acked', acked_at, message_id, agent_id
              FROM board.message_delivery WHERE acked_at > %(since)s AND acked_at <= %(until)s
            UNION ALL
            SELECT 'reply_timeout', reply_timeout_noted_at, id, sender_agent_id
              FROM board.message
             WHERE reply_timeout_noted_at > %(since)s AND reply_timeout_noted_at <= %(until)s
            ORDER BY at, message_id
            """,
            {"since": since, "until": until},
        )
        return cur.fetchall()


def _db_now(conn: psycopg.Connection) -> datetime:
    return conn.execute("SELECT now()").fetchone()[0]


def watch(conn: psycopg.Connection, *, history: int, once: bool) -> None:
    presence: dict[str, bool] = {}

    def show_presence_changes() -> None:
        for agent in registry.list_agents(conn):
            if presence.get(agent["agent_id"]) != agent["online"]:
                presence[agent["agent_id"]] = agent["online"]
                print(format_agent(agent))

    show_presence_changes()
    print(_c("2", "-" * 60))

    # Events are followed by the database's own clock, so a difference
    # between this machine's clock and the board's can't skip or repeat one.
    seen_until = _db_now(conn)
    last_id = 0
    for m in fetch_messages(conn, after_id=0, limit=history):
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
        for m in fetch_messages(conn, after_id=last_id):
            print(format_message(m))
            last_id = m["id"]
        now = _db_now(conn)
        for e in fetch_events(conn, since=seen_until, until=now):
            print(format_event(e))
        seen_until = now


def main() -> None:
    parser = argparse.ArgumentParser(description="Live feed of the agents' message board.")
    parser.add_argument("--history", type=int, default=20, help="recent messages to show first (default 20)")
    parser.add_argument("--once", action="store_true", help="print the current state and exit")
    args = parser.parse_args()

    dsn = os.environ.get("ORCH_BOARD_DSN")
    if not dsn:
        print("ORCH_BOARD_DSN is not set.", file=sys.stderr)
        sys.exit(1)
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            watch(conn, history=args.history, once=args.once)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
