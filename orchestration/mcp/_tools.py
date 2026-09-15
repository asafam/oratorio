"""Tool registration shared by both MCP transports (stdio and Streamable
HTTP) -- the actual tool logic lives once here and just delegates to
board_core; server.py and server_http.py differ only in transport and how
`get_sender` resolves the calling agent's identity.
"""
from __future__ import annotations

from typing import Any, Callable

from mcp.server.fastmcp import FastMCP

from orchestration.board_core import db, messages, registry


def register_tools(mcp: FastMCP, get_sender: Callable[[], str]) -> None:
    @mcp.tool()
    def post_message(
        content: str,
        recipient: str | None = None,
        topic: str | None = None,
        broadcast: bool = False,
        msg_type: str = "DOMAIN",
        in_reply_to: int | None = None,
        expects_reply: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Post a message to the board. Exactly one of recipient/topic/
        broadcast must be set. `sender` is never a parameter -- it is
        resolved server-side from your authenticated identity. To reply to
        a message, pass its `id` (from read_messages) as in_reply_to with
        msg_type='REPLY' -- never rely on "a reply just arrived from X",
        always thread by this id."""
        sender = get_sender()
        with db.get_pool().connection() as conn:
            result = messages.post_message(
                conn,
                sender,
                content=content,
                recipient=recipient,
                topic=topic,
                broadcast=broadcast,
                msg_type=msg_type,
                in_reply_to=in_reply_to,
                expects_reply=expects_reply,
                metadata=metadata,
            )
            conn.commit()
        return result

    @mcp.tool()
    def read_messages(
        unread_only: bool = True,
        topic: str | None = None,
        limit: int = 50,
        mark_read: bool = True,
    ) -> dict[str, Any]:
        """Read messages delivered to you. This is the durable path --
        independent of whether you were notified in real time, so call
        this on startup/wake regardless of why you woke up. Returns
        {"messages": [...]} -- an empty list means genuinely caught up,
        not "no response"."""
        sender = get_sender()
        with db.get_pool().connection() as conn:
            result = messages.read_messages(
                conn, sender, unread_only=unread_only, topic=topic, limit=limit,
                mark_read=mark_read,
            )
            conn.commit()
        return {"messages": result}

    @mcp.tool()
    def ack_message(message_id: int) -> dict[str, bool]:
        """Explicitly mark one message read, without consuming your whole
        unread queue -- use after read_messages(mark_read=False) if you
        want to peek before committing to having handled something."""
        sender = get_sender()
        with db.get_pool().connection() as conn:
            ok = messages.ack_message(conn, sender, message_id)
            conn.commit()
        return {"ok": ok}

    @mcp.tool()
    def subscribe(topic: str, backfill: bool = False) -> dict[str, bool]:
        """Subscribe to a topic. backfill=True also delivers recent history
        on that topic (off by default -- normal pub/sub semantics mean a
        late subscriber gets no backfill unless explicitly asked)."""
        sender = get_sender()
        with db.get_pool().connection() as conn:
            registry.subscribe(conn, sender, topic, backfill=backfill)
            conn.commit()
        return {"ok": True}

    @mcp.tool()
    def unsubscribe(topic: str) -> dict[str, bool]:
        sender = get_sender()
        with db.get_pool().connection() as conn:
            registry.unsubscribe(conn, sender, topic)
            conn.commit()
        return {"ok": True}

    @mcp.tool()
    def list_agents(active_only: bool = True) -> dict[str, Any]:
        with db.get_pool().connection() as conn:
            return {"agents": registry.list_agents(conn, active_only=active_only)}

    @mcp.tool()
    def get_role(agent_id: str | None = None) -> dict[str, Any] | None:
        """Look up a role's brief/peers/topics. Defaults to your own role;
        pass another agent_id to read a peer's brief (informational only,
        not an access grant)."""
        target = agent_id or get_sender()
        with db.get_pool().connection() as conn:
            return registry.get_role(conn, target)

    @mcp.tool()
    def list_topics() -> dict[str, Any]:
        with db.get_pool().connection() as conn:
            return {"topics": registry.list_topics(conn)}

    @mcp.tool()
    def read_all_messages(since_id: int = 0, limit: int = 200) -> dict[str, Any]:
        """Auditor-only full board history (bypasses your own inbox
        entirely). Refused unless your role is flagged is_auditor."""
        sender = get_sender()
        with db.get_pool().connection() as conn:
            return {"messages": registry.read_all_messages(conn, sender, since_id=since_id, limit=limit)}
