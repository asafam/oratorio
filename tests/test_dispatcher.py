"""Dispatcher integration test: the real LISTEN/NOTIFY loop, against a real
Postgres, waking a real (stub) HTTP receiver over a real socket -- the one
piece of the board not otherwise covered by the MCP/receiver/deliver tests.

Requires ORCH_BOARD_DSN with the schema applied. Skipped otherwise.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import psycopg
import pytest

from orchestration.board_core import auth, messages, registry
from orchestration.board_core.webhook_auth import SIGNATURE_HEADER, verify
from orchestration.dispatcher import listen

pytestmark = pytest.mark.skipif(
    not os.environ.get("ORCH_BOARD_DSN"),
    reason="ORCH_BOARD_DSN not set -- dispatcher tests need a real Postgres",
)


class _RecordingReceiver(BaseHTTPRequestHandler):
    calls: list[dict] = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        _RecordingReceiver.calls.append({
            "path": self.path,
            "body": body,
            "signature": self.headers.get(SIGNATURE_HEADER),
        })
        self.send_response(202)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"accepted": true, "queued": false}')

    def log_message(self, format, *args):
        pass  # keep test output quiet


@pytest.fixture
def stub_receiver():
    _RecordingReceiver.calls = []
    server = HTTPServer(("127.0.0.1", 0), _RecordingReceiver)
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}", _RecordingReceiver.calls
    server.shutdown()
    thread.join(timeout=5)


@pytest.fixture
def dsn():
    d = os.environ["ORCH_BOARD_DSN"]
    yield d
    with psycopg.connect(d, autocommit=True) as conn:
        conn.execute(
            "DELETE FROM board.message WHERE sender_agent_id LIKE 'toy-disp-%' "
            "OR recipient_agent_id LIKE 'toy-disp-%'"
        )
        conn.execute("DELETE FROM board.agent WHERE agent_id LIKE 'toy-disp-%'")


def test_post_message_wakes_real_receiver_via_dispatcher(dsn, stub_receiver):
    webhook_url, calls = stub_receiver
    secret = "dispatcher-test-secret"

    with psycopg.connect(dsn, autocommit=True) as conn:
        registry.upsert_agent(
            conn, agent_id="toy-disp-sender", role_doc_path="x", role_version="x",
            brief="x", peers=[], topics=[], auth_token_hash=auth.hash_token(auth.generate_token()),
        )
        registry.upsert_agent(
            conn, agent_id="toy-disp-recipient", role_doc_path="x", role_version="x",
            brief="x", peers=[], topics=[], auth_token_hash=auth.hash_token(auth.generate_token()),
            webhook_url=f"{webhook_url}/webhook/toy-disp-recipient", webhook_secret=secret,
        )

    async def scenario():
        dispatcher_task = asyncio.create_task(listen.run("http://fake-mcp"))
        await asyncio.sleep(0.5)  # let the dispatcher connect + start LISTENing

        with psycopg.connect(dsn, autocommit=True) as conn:
            result = messages.post_message(
                conn, "toy-disp-sender", recipient="toy-disp-recipient", content="wake up"
            )

        deadline = time.time() + 10
        while time.time() < deadline and not calls:
            await asyncio.sleep(0.2)

        dispatcher_task.cancel()
        try:
            await dispatcher_task
        except asyncio.CancelledError:
            pass
        return result

    result = asyncio.run(scenario())

    assert len(calls) == 1, f"expected exactly one webhook delivery, got {calls}"
    call = calls[0]
    assert call["path"] == "/webhook/toy-disp-recipient"

    # The real proof: signature actually verifies against the secret we
    # registered, and the payload references the real message id -- not a
    # hardcoded/fake value in this test.
    payload = verify(secret, call["body"], call["signature"])
    assert payload["message_id"] == result["message_id"]
