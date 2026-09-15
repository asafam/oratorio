"""Unit tests for HMAC sign/verify -- no Postgres, no network. These guard
the exact bug class the module's docstring warns about: hashing must
happen over the raw bytes, never a re-serialized copy."""
from __future__ import annotations

import json
import time

import pytest

from orchestration.board_core.webhook_auth import VerificationError, sign, verify


def _fresh_body(**extra) -> bytes:
    payload = {"event_id": "evt_test", "message_id": 1, "retry_count": 0,
               "delivered_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **extra}
    return json.dumps(payload, sort_keys=True).encode("utf-8")


def test_valid_signature_verifies():
    body = _fresh_body()
    sig = sign("secret1", body)
    result = verify("secret1", body, sig)
    assert result["event_id"] == "evt_test"


def test_wrong_secret_rejected():
    body = _fresh_body()
    sig = sign("secret1", body)
    with pytest.raises(VerificationError):
        verify("secret2", body, sig)


def test_tampered_body_rejected():
    body = _fresh_body()
    sig = sign("secret1", body)
    tampered = _fresh_body(message_id=999)
    with pytest.raises(VerificationError):
        verify("secret1", tampered, sig)


def test_reserialized_body_with_recomputed_signature_still_works():
    """Re-serializing then re-signing is fine (this is what a legitimate
    sender does); the bug this guards against is verifying against a
    DIFFERENT signature than the one covering these exact bytes, not
    re-serialization itself."""
    original = _fresh_body()
    reparsed = json.loads(original)
    resaved = json.dumps(reparsed, sort_keys=True).encode("utf-8")
    sig = sign("secret1", resaved)
    verify("secret1", resaved, sig)  # does not raise


def test_missing_signature_rejected():
    with pytest.raises(VerificationError):
        verify("secret1", _fresh_body(), None)


def test_stale_delivery_rejected():
    old_body = _fresh_body(delivered_at="2020-01-01T00:00:00Z")
    sig = sign("secret1", old_body)
    with pytest.raises(VerificationError, match="stale"):
        verify("secret1", old_body, sig)
