"""Tool-level tests: drive the real orchestration MCP server (stdio
transport) as a subprocess over the actual MCP protocol -- not just calling
board_core functions directly (that's tests/test_board.py).
This is what actually proves a Claude Code / Codex CLI process connected
via .mcp.json would work.

Requires a real Postgres reachable at ORCH_BOARD_DSN with the schema
applied. Skipped entirely if that's not set.
"""
from __future__ import annotations

import asyncio
import os
import sys

import psycopg
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from orchestration.board_core import auth, registry

pytestmark = pytest.mark.skipif(
    not os.environ.get("ORCH_BOARD_DSN"),
    reason="ORCH_BOARD_DSN not set -- orchestration MCP tests need a real Postgres",
)


def _make_agent(dsn: str, agent_id: str, topics: list[str] | None = None) -> str:
    token = auth.generate_token()
    with psycopg.connect(dsn, autocommit=True) as conn:
        registry.upsert_agent(
            conn,
            agent_id=agent_id,
            role_doc_path=f"orchestration/roles/{agent_id}.md",
            role_version="test",
            brief=f"toy mcp agent {agent_id}",
            peers=[],
            topics=topics or [],
            auth_token_hash=auth.hash_token(token),
        )
    return token


def _cleanup(dsn: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "DELETE FROM board.message WHERE sender_agent_id LIKE 'toy-mcp-%' "
            "OR recipient_agent_id LIKE 'toy-mcp-%'"
        )
        conn.execute("DELETE FROM board.agent WHERE agent_id LIKE 'toy-mcp-%'")


async def _call(dsn: str, token: str, tool: str, args: dict):
    # Use structuredContent, not content[0].text: FastMCP's text rendering
    # of a list-returning tool is not reliably a JSON array (e.g. a
    # single-item list renders as that one item's own JSON object), so
    # content[0].text is not safe to json.loads() generically.
    # structuredContent wraps a list-typed tool's output as {"result": [...]}
    # per the MCP output-schema convention; a dict-typed tool's output is
    # the dict itself, unwrapped.
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "orchestration.mcp.server"],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        env={"ORCH_BOARD_DSN": dsn, "ORCH_AGENT_TOKEN": token, "PATH": os.environ["PATH"]},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool, args)
            if result.isError:
                raise RuntimeError(result.content[0].text if result.content else "tool call failed")
            return result.structuredContent


@pytest.fixture
def dsn():
    d = os.environ["ORCH_BOARD_DSN"]
    yield d
    _cleanup(d)


def test_post_then_read_over_real_mcp_protocol(dsn):
    token_a = _make_agent(dsn, "toy-mcp-a")
    token_b = _make_agent(dsn, "toy-mcp-b")

    posted = asyncio.run(
        _call(dsn, token_a, "post_message", {"recipient": "toy-mcp-b", "content": "hi from a"})
    )
    assert posted["delivered_to"] == ["toy-mcp-b"]

    inbox = asyncio.run(_call(dsn, token_b, "read_messages", {}))["messages"]
    assert any(m["id"] == posted["message_id"] and m["content"] == "hi from a" for m in inbox)

    # sender identity came from the token, NEVER a caller-supplied field --
    # confirm the delivered message's sender is toy-mcp-a even though the
    # post_message call never mentioned it.
    assert all(m["sender"] == "toy-mcp-a" for m in inbox if m["id"] == posted["message_id"])


def test_invalid_token_is_rejected(dsn):
    with pytest.raises(Exception):
        asyncio.run(_call(dsn, "not-a-real-token", "list_agents", {}))
