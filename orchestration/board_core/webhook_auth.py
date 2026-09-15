"""HMAC signing/verification shared by the dispatcher (signs) and the
receiver (verifies), so the two sides can never silently drift apart on
the signing scheme. Deliberately separate from board_core/auth.py (which
is about MCP bearer-token identity, a different secret and a different
direction of trust -- see the schema comment on board.agent.webhook_secret).
"""
from __future__ import annotations

import calendar
import hashlib
import hmac
import json
import time

SIGNATURE_HEADER = "X-Board-Signature"
MAX_CLOCK_SKEW_SECONDS = 300  # replay-protection window


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


class VerificationError(Exception):
    pass


def verify(secret: str, body: bytes, signature_header: str | None) -> dict:
    """Verify an inbound webhook body against its X-Board-Signature header AND
    freshness (delivered_at inside the body, which is itself covered by the
    signature -- so it can only be trusted AFTER the HMAC check passes).
    Returns the parsed payload on success, raises VerificationError
    otherwise. Hashes the RAW bytes -- never re-serialize-then-hash, which
    would silently accept a signature computed over a differently-ordered
    (but semantically identical) JSON encoding.
    """
    if not signature_header:
        raise VerificationError("missing signature header")
    expected = sign(secret, body)
    if not hmac.compare_digest(expected, signature_header):
        raise VerificationError("signature mismatch")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as e:
        raise VerificationError(f"invalid JSON body: {e}") from e

    delivered_at = payload.get("delivered_at")
    if delivered_at:
        try:
            # calendar.timegm, NOT time.mktime: the timestamp is UTC ("...Z"),
            # and mktime interprets a struct_time as LOCAL time -- on any
            # host not itself in UTC that silently miscomputes skew by the
            # local offset, which could reject valid deliveries or (worse)
            # accept stale/replayed ones depending on the sign of the offset.
            sent_ts = calendar.timegm(time.strptime(delivered_at, "%Y-%m-%dT%H:%M:%SZ"))
        except ValueError as e:
            raise VerificationError(f"unparseable delivered_at: {e}") from e
        skew = abs(time.time() - sent_ts)
        if skew > MAX_CLOCK_SKEW_SECONDS:
            raise VerificationError(f"stale delivery: {skew:.0f}s old (max {MAX_CLOCK_SKEW_SECONDS}s)")

    return payload
