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
        expires_in_seconds: int | None = None,
        reply_within_seconds: int | None = None,
        thread_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Post a message to the board. Exactly one of recipient/topic/
        broadcast must be set. `sender` is never a parameter -- it is
        resolved server-side from your authenticated identity. To reply to
        a message, pass its `id` as in_reply_to with msg_type='REPLY' --
        never rely on "a reply just arrived from X", always thread by this
        id.

        The recipient may be down for minutes or hours, so decide whether
        this message should wait for it: by default it waits as long as it
        takes. Set expires_in_seconds if it only matters now (it is dropped
        if not handled by then). Set reply_within_seconds if you need an
        answer by a deadline -- you are told once if none arrived.

        thread_id groups messages about the same piece of work; a reply
        inherits its parent's. Start a new one for unrelated work."""
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
                expires_in_seconds=expires_in_seconds,
                reply_within_seconds=reply_within_seconds,
                thread_id=thread_id,
                metadata=metadata,
            )
            conn.commit()
        return result

    @mcp.tool()
    def read_messages(
        pending_only: bool = True,
        topic: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Read your inbox. Reading never consumes a message -- it stays
        pending until you ack_message it. New messages are normally handed
        to you as they arrive; use this to re-check what is still open
        (e.g. after your context was cleared or compacted), or pass
        pending_only=False for history. Returns {"messages": [...]} -- an
        empty list means genuinely caught up, not "no response"."""
        sender = get_sender()
        with db.get_pool().connection() as conn:
            result = messages.read_messages(
                conn, sender, pending_only=pending_only, topic=topic, limit=limit,
            )
        return {"messages": result}

    @mcp.tool()
    def ack_message(message_id: int) -> dict[str, bool]:
        """Mark one message done. Call this when you have finished acting
        on a message -- until then it stays pending and is handed to you
        again if your session starts over."""
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
        """Who is registered, what each one is (runner), and whether it is
        online right now. An offline agent still receives messages -- they
        wait in its inbox until it is back."""
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
