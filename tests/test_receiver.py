"""Receiver tests: single-flight locking, queue-coalescing, and the HTTP
signature/routing layer. No Postgres, no real claude/codex -- the runner
and the drain check (board_client.is_caught_up) are stubbed so this
exercises exactly the receiver's own logic in isolation, without needing
real infrastructure.
"""
from __future__ import annotations

import json
import shutil
import threading
import time

import pytest
from fastapi.testclient import TestClient

from orchestration.board_core.webhook_auth import SIGNATURE_HEADER, sign
from orchestration.receiver.config import RoleConfig
from orchestration.receiver.lock import LOCK_DIR
from orchestration.receiver.runners import RunResult
from orchestration.receiver.server import ReceiverApp, build_app


def _role_cfg(role: str, **overrides) -> RoleConfig:
    defaults = dict(
        role=role, webhook_secret=f"secret-{role}", agent_token=f"token-{role}",
        runner="stub", brief_path="orchestration/roles/monitor.md", max_runtime_minutes=1,
    )
    defaults.update(overrides)
    return RoleConfig(**defaults)


@pytest.fixture(autouse=True)
def _clean_lock_dir():
    yield
    shutil.rmtree(LOCK_DIR, ignore_errors=True)


def test_single_run_drains_immediately_when_caught_up(monkeypatch):
    calls = []

    def stub_runner(prompt, **kwargs):
        calls.append(prompt)
        return RunResult(0, "ok", "")

    monkeypatch.setattr(
        "orchestration.receiver.server.board_client.is_caught_up",
        lambda mcp_url, token: _async_true(),
    )

    receiver = ReceiverApp(
        {"monitor": _role_cfg("monitor")}, mcp_url="http://fake", runners={"stub": stub_runner}
    )
    result = receiver.handle_wakeup("monitor", "evt_1")
    assert result == {"accepted": True, "queued": False, "event_id": "evt_1"}

    receiver.executor.shutdown(wait=True)
    assert len(calls) == 1


def test_concurrent_wakeup_while_busy_is_queued_then_coalesced(monkeypatch):
    calls = []
    release_first_call = threading.Event()

    def stub_runner(prompt, **kwargs):
        calls.append(prompt)
        if len(calls) == 1:
            release_first_call.wait(timeout=5)
        return RunResult(0, "ok", "")

    # First drain check (after run #1) says NOT caught up (a second wakeup
    # arrived while busy) -- second drain check (after run #2) says caught up.
    drain_responses = iter([False, True])
    monkeypatch.setattr(
        "orchestration.receiver.server.board_client.is_caught_up",
        lambda mcp_url, token: _async_value(next(drain_responses)),
    )

    receiver = ReceiverApp(
        {"monitor": _role_cfg("monitor")}, mcp_url="http://fake", runners={"stub": stub_runner}
    )
    first = receiver.handle_wakeup("monitor", "evt_1")
    assert first == {"accepted": True, "queued": False, "event_id": "evt_1"}

    # Give the background thread a moment to actually start (acquire the lock)
    # before the second wakeup arrives, so it genuinely observes "busy".
    time.sleep(0.2)
    second = receiver.handle_wakeup("monitor", "evt_2")
    assert second == {"accepted": True, "queued": True, "event_id": "evt_2"}

    release_first_call.set()
    receiver.executor.shutdown(wait=True)

    # Exactly TWO runs: the original run, plus ONE coalesced follow-up --
    # never one run per queued wakeup.
    assert len(calls) == 2


def test_webhook_route_rejects_bad_signature():
    receiver = ReceiverApp({"monitor": _role_cfg("monitor")}, mcp_url="http://fake",
                            runners={"stub": lambda *a, **k: RunResult(0, "", "")})
    app = build_app(receiver)
    client = TestClient(app)

    body = json.dumps({"event_id": "evt_1", "message_id": 1, "retry_count": 0,
                        "delivered_at": "2026-01-01T00:00:00Z"}).encode()
    resp = client.post("/webhook/monitor", content=body,
                        headers={SIGNATURE_HEADER: "sha256=deadbeef"})
    assert resp.status_code == 401


def test_webhook_route_unknown_role_is_404():
    receiver = ReceiverApp({"monitor": _role_cfg("monitor")}, mcp_url="http://fake",
                            runners={"stub": lambda *a, **k: RunResult(0, "", "")})
    app = build_app(receiver)
    client = TestClient(app)
    resp = client.post("/webhook/nonexistent-role", content=b"{}")
    assert resp.status_code == 404


def test_webhook_route_accepts_valid_signature(monkeypatch):
    monkeypatch.setattr(
        "orchestration.receiver.server.board_client.is_caught_up",
        lambda mcp_url, token: _async_true(),
    )
    receiver = ReceiverApp({"monitor": _role_cfg("monitor")}, mcp_url="http://fake",
                            runners={"stub": lambda *a, **k: RunResult(0, "", "")})
    app = build_app(receiver)
    client = TestClient(app)

    body = json.dumps({
        "event_id": "evt_1", "message_id": 1, "retry_count": 0,
        "delivered_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }, sort_keys=True).encode()
    sig = sign("secret-monitor", body)
    resp = client.post("/webhook/monitor", content=body, headers={SIGNATURE_HEADER: sig})
    assert resp.status_code == 202
    assert resp.json()["queued"] is False
    receiver.executor.shutdown(wait=True)


async def _async_true():
    return True


async def _async_value(v):
    return v
