#!/usr/bin/env python3
"""Orchestration board MCP server -- stdio transport.

For local development/testing directly on the board-host (no network hop).
None of the five agent roles use this transport in practice once deployed
-- they're all remote clients of the board-host and use server_http.py
instead. See orchestration/mcp/mcp_config.example.json.

Identity: stdio is process-local and already trusted (whatever launched
this subprocess controls its environment), so the caller's identity is
resolved ONCE at startup from ORCH_AGENT_TOKEN and cached for the life of
the process -- there is no per-call HTTP header to re-check. Fails fast if
the token is missing/invalid rather than silently running unauthenticated.
"""
from __future__ import annotations

import os
import sys

from mcp.server.fastmcp import FastMCP

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from orchestration.board_core import auth, db  # noqa: E402
from orchestration.mcp._tools import register_tools  # noqa: E402


def _resolve_identity_at_startup() -> str:
    token = os.environ.get("ORCH_AGENT_TOKEN")
    if not token:
        print("ORCH_AGENT_TOKEN is not set -- refusing to start unauthenticated.", file=sys.stderr)
        sys.exit(1)
    with db.get_pool().connection() as conn:
        try:
            return auth.resolve_sender(conn, token)
        except auth.AuthError as e:
            print(f"ORCH_AGENT_TOKEN rejected: {e}", file=sys.stderr)
            sys.exit(1)


mcp = FastMCP("orchestration-board")
_identity = _resolve_identity_at_startup()
register_tools(mcp, get_sender=lambda: _identity)

if __name__ == "__main__":
    mcp.run(transport="stdio")
