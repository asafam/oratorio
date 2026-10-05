"""post_message / read_messages / ack_message -- the durable-queue core.

A delivery moves through two steps: `delivered_at` (the agent's listener
handed the message to its session) and `acked_at` (the agent finished with
it). Only an ack consumes a message -- reading never does -- so a session
that crashes or is cleared mid-task loses nothing. "Still pending" is
defined once, by the board.pending_delivery view (not acked, not expired).

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
    expires_in_seconds: int | None = None,
    reply_within_seconds: int | None = None,
    thread_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Insert a message and return {message_id, delivered_to}.

    The sender decides per message whether it waits for a recipient that
    is down: `expires_in_seconds` drops it if nobody has acked it by then;
    `reply_within_seconds` is the sender's deadline for an answer (implies
    expects_reply -- see claim_overdue_requests). Neither set means it
    waits in the inbox as long as it takes. Both are turned into absolute
    times by the database's own clock, never the caller's.

    `thread_id` groups related messages; a reply inherits its parent's
    unless one is given explicitly.

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

    if reply_within_seconds is not None:
        expects_reply = True

    depth_remaining = DEFAULT_DEPTH
    if in_reply_to is not None:
        parent = conn.execute(
            "SELECT depth_remaining, thread_id FROM board.message WHERE id = %s", (in_reply_to,)
        ).fetchone()
        if parent is None:
            raise RoutingError(f"in_reply_to={in_reply_to} does not exist")
        depth_remaining = max(parent[0] - 1, 0)
        if thread_id is None:
            thread_id = parent[1]

    row = conn.execute(
        """
        INSERT INTO board.message
            (sender_agent_id, recipient_agent_id, topic, is_broadcast,
             msg_type, content, in_reply_to, depth_remaining, expects_reply,
             expires_at, reply_by, thread_id, metadata)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                now() + make_interval(secs => %s), now() + make_interval(secs => %s), %s, %s)
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
            expires_in_seconds,
            reply_within_seconds,
            thread_id,
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


_MESSAGE_COLUMNS = """
    m.id, m.sender_agent_id AS sender, m.recipient_agent_id AS recipient,
    m.topic, m.is_broadcast, m.msg_type, m.content, m.in_reply_to,
    m.expects_reply, m.expires_at, m.reply_by, m.thread_id, m.metadata, m.created_at
"""


def read_messages(
    conn: Connection,
    agent_id: str,
    *,
    pending_only: bool = True,
    topic: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Read this agent's inbox -- the durable path, independent of whether
    a NOTIFY was ever received for these messages. Never consumes anything:
    a message stays pending until ack_message. pending_only=False also
    returns already-acked and expired messages (history)."""
    source = "board.pending_delivery" if pending_only else "board.message_delivery"
    conditions = ["d.agent_id = %s"]
    params: list[Any] = [agent_id]
    if topic is not None:
        conditions.append("m.topic = %s")
        params.append(topic)
    where = " AND ".join(conditions)
    params.append(limit)

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            SELECT {_MESSAGE_COLUMNS}
            FROM {source} d
            JOIN board.message m ON m.id = d.message_id
            WHERE {where}
            ORDER BY m.id
            LIMIT %s
            """,
            params,
        )
        return cur.fetchall()


def ack_message(conn: Connection, agent_id: str, message_id: int) -> bool:
    """Mark one message done for this agent. The only thing that consumes
    a message."""
    cur = conn.execute(
        "UPDATE board.message_delivery SET acked_at = now() "
        "WHERE agent_id = %s AND message_id = %s AND acked_at IS NULL",
        (agent_id, message_id),
    )
    return cur.rowcount > 0


def is_caught_up(conn: Connection, agent_id: str) -> bool:
    """True if agent_id has nothing pending."""
    row = conn.execute(
        "SELECT NOT EXISTS (SELECT 1 FROM board.pending_delivery WHERE agent_id = %s)",
        (agent_id,),
    ).fetchone()
    return bool(row[0])


def undelivered(conn: Connection, agent_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
    """Pending messages the listener has not yet handed to the session --
    its work list. Pair with mark_delivered once the hand-off succeeded."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            SELECT {_MESSAGE_COLUMNS}
            FROM board.pending_delivery d
            JOIN board.message m ON m.id = d.message_id
            WHERE d.agent_id = %s AND d.delivered_at IS NULL
            ORDER BY m.id
            LIMIT %s
            """,
            (agent_id, limit),
        )
        return cur.fetchall()


def mark_delivered(conn: Connection, agent_id: str, message_ids: list[int]) -> None:
    conn.execute(
        "UPDATE board.message_delivery SET delivered_at = now() "
        "WHERE agent_id = %s AND message_id = ANY(%s) AND delivered_at IS NULL",
        (agent_id, message_ids),
    )


def requeue_unacked(conn: Connection, agent_id: str) -> int:
    """Put every handed-over-but-never-acked message back on the listener's
    work list. Called when a session starts over (listener start, or after
    a /clear) -- whatever it was holding is gone from its context, so it
    must be handed in again. Delivery is therefore at-least-once: an agent
    can see the same message id twice."""
    cur = conn.execute(
        "UPDATE board.message_delivery SET delivered_at = NULL "
        "WHERE agent_id = %s AND acked_at IS NULL AND delivered_at IS NOT NULL",
        (agent_id,),
    )
    return cur.rowcount


def claim_overdue_requests(conn: Connection, agent_id: str) -> list[dict[str, Any]]:
    """Requests this agent sent whose reply_by has passed with no REPLY.
    Each is returned exactly once (stamped reply_timeout_noted_at in the
    same statement), so the sender's listener can tell it "timed out" one
    time and let it decide what to do next."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            UPDATE board.message m SET reply_timeout_noted_at = now()
            WHERE m.sender_agent_id = %s
              AND m.reply_by IS NOT NULL AND m.reply_by <= now()
              AND m.reply_timeout_noted_at IS NULL
              AND NOT EXISTS (
                    SELECT 1 FROM board.message r
                    WHERE r.in_reply_to = m.id AND r.msg_type = 'REPLY'
                  )
            RETURNING m.id, m.recipient_agent_id AS recipient, m.topic, m.is_broadcast,
                      m.content, m.reply_by, m.thread_id
            """,
            (agent_id,),
        )
        return cur.fetchall()
