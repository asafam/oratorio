"""post_message / read_messages / ack_message -- the durable-queue core.

Every function here takes an already-open psycopg Connection so callers
(the MCP tool handlers, tests, toy Phase-0 scripts) control transaction
boundaries explicitly. Nothing in this module is transport-aware (no MCP,
no HTTP) -- see orchestration/mcp/server.py for the transport layer.
"""
from __future__ import annotations

from typing import Any

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

DEFAULT_DEPTH = 8


class RoutingError(ValueError):
    pass


def post_message(
    conn: Connection,
    sender_agent_id: str,
    *,
    content: str,
    recipient: str | None = None,
    topic: str | None = None,
    broadcast: bool = False,
    msg_type: str = "DOMAIN",
    in_reply_to: int | None = None,
    expects_reply: bool = False,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Insert a message and return {message_id, delivered_to}.

    Exactly one of recipient/topic/broadcast must be set -- validated here
    (defense in depth) and by the DB's exactly_one_route CHECK constraint.
    `sender_agent_id` must already be resolved from an authenticated token
    (see auth.resolve_sender) -- this function does not verify identity.
    """
    routes_set = sum(bool(x) for x in (recipient, topic, broadcast))
    if routes_set != 1:
        raise RoutingError(
            "exactly one of recipient/topic/broadcast must be set "
            f"(got recipient={recipient!r}, topic={topic!r}, broadcast={broadcast!r})"
        )
    if msg_type == "REPLY" and in_reply_to is None:
        raise RoutingError("msg_type='REPLY' requires in_reply_to")

    depth_remaining = DEFAULT_DEPTH
    if in_reply_to is not None:
        parent = conn.execute(
            "SELECT depth_remaining FROM board.message WHERE id = %s", (in_reply_to,)
        ).fetchone()
        if parent is None:
            raise RoutingError(f"in_reply_to={in_reply_to} does not exist")
        depth_remaining = max(parent[0] - 1, 0)

    row = conn.execute(
        """
        INSERT INTO board.message
            (sender_agent_id, recipient_agent_id, topic, is_broadcast,
             msg_type, content, in_reply_to, depth_remaining, expects_reply, metadata)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            sender_agent_id,
            recipient,
            topic,
            broadcast,
            msg_type,
            content,
            in_reply_to,
            depth_remaining,
            expects_reply,
            Jsonb(metadata or {}),
        ),
    ).fetchone()
    message_id = row[0]

    delivered_to = [
        r[0]
        for r in conn.execute(
            "SELECT agent_id FROM board.message_delivery WHERE message_id = %s",
            (message_id,),
        ).fetchall()
    ]
    return {"message_id": message_id, "delivered_to": delivered_to}


def read_messages(
    conn: Connection,
    agent_id: str,
    *,
    unread_only: bool = True,
    topic: str | None = None,
    limit: int = 50,
    mark_read: bool = True,
) -> list[dict[str, Any]]:
    """Read from board.message_delivery -- the durable path, independent of
    whether a NOTIFY was ever received for these messages."""
    conditions = ["d.agent_id = %s"]
    params: list[Any] = [agent_id]
    if unread_only:
        conditions.append("d.read_at IS NULL")
    if topic is not None:
        conditions.append("m.topic = %s")
        params.append(topic)
    where = " AND ".join(conditions)
    params.append(limit)

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            SELECT m.id, m.sender_agent_id AS sender, m.recipient_agent_id AS recipient,
                   m.topic, m.is_broadcast, m.msg_type, m.content, m.in_reply_to,
                   m.expects_reply, m.metadata, m.created_at
            FROM board.message_delivery d
            JOIN board.message m ON m.id = d.message_id
            WHERE {where}
            ORDER BY m.id
            LIMIT %s
            """,
            params,
        )
        rows = cur.fetchall()

    if mark_read and rows:
        ids = [r["id"] for r in rows]
        conn.execute(
            "UPDATE board.message_delivery SET read_at = now() "
            "WHERE agent_id = %s AND message_id = ANY(%s) AND read_at IS NULL",
            (agent_id, ids),
        )
    return rows


def ack_message(conn: Connection, agent_id: str, message_id: int) -> bool:
    """Explicitly mark one message read for this agent, independent of
    read_messages' own mark_read default -- lets an agent peek without
    consuming when it calls read_messages(mark_read=False)."""
    cur = conn.execute(
        "UPDATE board.message_delivery SET read_at = now() "
        "WHERE agent_id = %s AND message_id = %s AND read_at IS NULL",
        (agent_id, message_id),
    )
    return cur.rowcount > 0


def is_caught_up(conn: Connection, agent_id: str) -> bool:
    """True if agent_id has no unread deliveries. This is the single cursor
    the whole system uses for 'has this role finished its pending work' --
    e.g. the receiver's drain-after-a-run decision -- never a second,
    receiver-local 'last processed id' file (see orchestration/receiver)."""
    row = conn.execute(
        "SELECT NOT EXISTS ("
        "  SELECT 1 FROM board.message_delivery WHERE agent_id = %s AND read_at IS NULL"
        ")",
        (agent_id,),
    ).fetchone()
    return bool(row[0])


def pending_recipients(conn: Connection, message_id: int) -> list[str]:
    """Who still needs to see this message -- the dispatcher's wake list.
    Derived from message_delivery (the fan-out trigger's own output), never
    re-parsed out of a NOTIFY channel name, so it's correct for
    direct/topic/broadcast alike."""
    return [
        r[0]
        for r in conn.execute(
            "SELECT agent_id FROM board.message_delivery "
            "WHERE message_id = %s AND read_at IS NULL",
            (message_id,),
        ).fetchall()
    ]
