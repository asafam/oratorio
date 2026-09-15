#!/usr/bin/env python3
"""Orchestration board MCP server -- Streamable HTTP transport.

This is the transport every agent role actually uses in practice (every
local role and the remote overseer role are all remote clients of the
board-host). Runs on the board-host, reachable through that host's own
Cloudflare Tunnel -- see orchestration/README.md.

Auth: a per-role bearer token (Authorization: Bearer <token>), verified
against board.agent.auth_token_hash via the mcp SDK's TokenVerifier
protocol -- NOT a real OAuth flow (roles hold a pre-shared token, they
never go through an authorization-code exchange). AuthSettings.issuer_url/
resource_server_url are required fields by the SDK's types but are not
acting as a real OAuth issuer here; they're set from ORCH_MCP_PUBLIC_URL
purely to satisfy the type and label the resource server in any metadata
FastMCP exposes.

VERIFY AT DEPLOY TIME: this wiring (TokenVerifier + AuthSettings enabling
FastMCP's auth middleware, and get_access_token() surfacing our resolved
identity inside a tool call) was written against the mcp SDK version
pinned in requirements.txt but has only been exercised via the stdio
transport so far (no board-host exists yet to deploy this to) -- confirm
end to end against a real deployment before relying on it, per the
project's "verify before implementing" list.
"""
from __future__ import annotations

import asyncio
import os
import sys

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from pydantic import AnyHttpUrl

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from orchestration.board_core import auth, db  # noqa: E402
from orchestration.mcp._tools import register_tools  # noqa: E402


class BoardTokenVerifier(TokenVerifier):
    async def verify_token(self, token: str) -> AccessToken | None:
        def _resolve() -> str | None:
            with db.get_pool().connection() as conn:
                try:
                    return auth.resolve_sender(conn, token)
                except auth.AuthError:
                    return None

        agent_id = await asyncio.to_thread(_resolve)
        if agent_id is None:
            return None
        return AccessToken(token=token, client_id=agent_id, scopes=[])


def _get_sender() -> str:
    access_token = get_access_token()
    if access_token is None:
        # Should be unreachable -- FastMCP's auth middleware rejects
        # unauthenticated requests before a tool ever runs. A tool
        # reaching this means the middleware wiring is broken, not that
        # the caller lacks a token -- fail loudly rather than silently.
        raise RuntimeError("no authenticated identity in request context")
    return access_token.client_id


def build_app() -> FastMCP:
    public_url = os.environ.get("ORCH_MCP_PUBLIC_URL")
    if not public_url:
        print("ORCH_MCP_PUBLIC_URL is not set (e.g. the board-host's Cloudflare Tunnel "
              "hostname) -- refusing to start.", file=sys.stderr)
        sys.exit(1)

    mcp = FastMCP(
        "orchestration-board",
        host="127.0.0.1",  # never bind a public interface directly -- the tunnel does that
        port=int(os.environ.get("ORCH_MCP_PORT", "8300")),
        token_verifier=BoardTokenVerifier(),
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(public_url),
            resource_server_url=AnyHttpUrl(public_url),
        ),
    )
    register_tools(mcp, get_sender=_get_sender)
    return mcp


if __name__ == "__main__":
    app = build_app()
    app.run(transport="streamable-http")
