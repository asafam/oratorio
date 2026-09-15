"""Token issuance/verification for the orchestration board.

sender identity is ALWAYS resolved server-side from an authenticated bearer
token -- never accepted as a caller-supplied parameter (post_message has no
`sender` argument). A sender field taken from the request body could be
forged by a bug or a malicious prompt; deriving it from the verified token
makes that structurally impossible instead of merely policy-discouraged.
"""
from __future__ import annotations

import hashlib
import secrets

from psycopg import Connection


class AuthError(Exception):
    pass


def generate_token() -> str:
    """A fresh, high-entropy bearer token for a role. Distribute out of
    band (the role's own launch environment) -- never commit it."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def resolve_sender(conn: Connection, token: str) -> str:
    """Return the agent_id whose auth_token_hash matches `token`, or raise
    AuthError. This is the ONLY place sender identity is established."""
    if not token:
        raise AuthError("missing bearer token")
    token_hash = hash_token(token)
    row = conn.execute(
        "SELECT agent_id FROM board.agent WHERE auth_token_hash = %s AND active",
        (token_hash,),
    ).fetchone()
    if row is None:
        raise AuthError("invalid or inactive token")
    return row[0]
