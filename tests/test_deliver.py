"""Dispatcher delivery tests: payload/signature shape and the
fail-fast-on-permanent-errors behavior of deliver_with_retry. Uses a fake
httpx transport (no real sockets) so these run instantly and
deterministically -- the live-socket path was verified manually against a
real receiver process during development (see orchestration/README.md)."""
from __future__ import annotations

import httpx
import pytest

from orchestration.board_core.webhook_auth import verify
from orchestration.dispatcher import deliver


def test_build_payload_signs_and_verifies_round_trip():
    body = deliver.build_payload(message_id=7, retry_count=0, mcp_url="http://x")
    sig = deliver.sign("s3cr3t", body)
    result = verify("s3cr3t", body, sig)
    assert result["message_id"] == 7


def test_permanent_failure_does_not_retry(monkeypatch):
    calls = []

    def fake_deliver_once(url, secret, body, timeout=deliver.REQUEST_TIMEOUT_SECONDS):
        calls.append(1)
        return httpx.Response(401, text="signature mismatch", request=httpx.Request("POST", url))

    monkeypatch.setattr(deliver, "deliver_once", fake_deliver_once)
    result = deliver.deliver_with_retry("http://fake/webhook/monitor", "bad-secret", message_id=1)

    assert result.delivered is False
    assert result.attempts == 1  # NOT the full 5-attempt retry schedule
    assert "permanent" in result.error


def test_transient_failure_retries_then_succeeds(monkeypatch):
    calls = []

    def fake_deliver_once(url, secret, body, timeout=deliver.REQUEST_TIMEOUT_SECONDS):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(503, text="try again", request=httpx.Request("POST", url))
        return httpx.Response(202, text="ok", request=httpx.Request("POST", url))

    monkeypatch.setattr(deliver, "deliver_once", fake_deliver_once)
    monkeypatch.setattr(deliver.time, "sleep", lambda s: None)  # skip real backoff delays in the test

    result = deliver.deliver_with_retry("http://fake/webhook/monitor", "secret", message_id=1)
    assert result.delivered is True
    assert result.attempts == 3
