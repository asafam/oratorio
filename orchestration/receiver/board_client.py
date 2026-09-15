"""The receiver's own lightweight MCP client -- used only to check the
drain condition after a run finishes (does this role still have unread
messages?). Deliberately goes over the SAME network path (Streamable HTTP
to the board-host, same bearer token) a launched claude/codex process
would use -- the receiver never gets a direct Postgres connection, even
though the underlying question ("is role X caught up?") is answered by a
single row in board.message_delivery on the other end. See
orchestration/README.md for why local-role hosts never get direct DB access.
"""
from __future__ import annotations

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


async def is_caught_up(mcp_url: str, token: str) -> bool:
    async with streamablehttp_client(mcp_url, headers={"Authorization": f"Bearer {token}"}) as (
        read, write, _get_session_id,
    ):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "read_messages", {"unreadOnly": True, "limit": 1, "markRead": False}
            )
            if result.isError:
                # Fail closed on the side of "assume more work" -- a
                # transient MCP error should trigger a harmless extra
                # drain-check next time, never silently stop draining.
                return False
            messages = (result.structuredContent or {}).get("messages", [])
            return len(messages) == 0
