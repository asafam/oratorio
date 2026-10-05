"""Listener integration test: the real LISTEN/NOTIFY loop, against a real
Postgres, handing a posted message to a recording handler that stands in
for the agent's session.

Requires ORCH_BOARD_DSN with the schema applied. Skipped otherwise.
"""
from __future__ import annotations

import asyncio
import os
import time

import psycopg
import pytest

from orchestration.board_core import auth, messages, registry
from orchestration.listener import listen

pytestmark = pytest.mark.skipif(
    not os.environ.get("ORCH_BOARD_DSN"),
    reason="ORCH_BOARD_DSN not set -- listener tests need a real Postgres",
)


@pytest.fixture
def dsn():
    d = os.environ["ORCH_BOARD_DSN"]
    with psycopg.connect(d, autocommit=True) as conn:
        for agent_id in ("toy-lis-sender", "toy-lis-recipient"):
            registry.upsert_agent(
                conn, agent_id=agent_id, role_doc_path="x", role_version="x", brief="x",
                peers=[], topics=[], auth_token_hash=auth.hash_token(auth.generate_token()),
            )
    yield d
    with psycopg.connect(d, autocommit=True) as conn:
        conn.execute(
            "DELETE FROM board.message WHERE sender_agent_id LIKE 'toy-lis-%' "
            "OR recipient_agent_id LIKE 'toy-lis-%'"
        )
        conn.execute("DELETE FROM board.agent WHERE agent_id LIKE 'toy-lis-%'")


def _run_listener_until(dsn: str, handed: list[dict], post, done) -> None:
    listener = listen.Listener("toy-lis-recipient", "claude", handler=handed.append)

    async def scenario():
        task = asyncio.create_task(listen.run(listener))
        await asyncio.sleep(0.5)  # let it connect + start LISTENing
        post()
        deadline = time.time() + 10
        while time.time() < deadline and not done():
            await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())


def test_posted_message_is_handed_to_the_session_once(dsn):
    handed: list[dict] = []
    posted: dict = {}

    def post():
        with psycopg.connect(dsn, autocommit=True) as conn:
            posted.update(messages.post_message(
                conn, "toy-lis-sender", recipient="toy-lis-recipient", content="hello",
                thread_id="toy-lis-thread",
            ))

    _run_listener_until(dsn, handed, post, lambda: bool(handed))

    assert [h["id"] for h in handed] == [posted["message_id"]]
    assert handed[0]["kind"] == "message" and handed[0]["new_thread"] is True

    with psycopg.connect(dsn, autocommit=True) as conn:
        # Handed over, but still pending until the agent acks it.
        assert messages.undelivered(conn, "toy-lis-recipient") == []
        assert not messages.is_caught_up(conn, "toy-lis-recipient")
        agents = {a["agent_id"]: a for a in registry.list_agents(conn)}
        assert agents["toy-lis-recipient"]["online"] is True


def test_message_posted_while_listener_was_down_is_handed_over_on_start(dsn):
    with psycopg.connect(dsn, autocommit=True) as conn:
        msg_id = messages.post_message(
            conn, "toy-lis-sender", recipient="toy-lis-recipient", content="while you were out"
        )["message_id"]

    handed: list[dict] = []
    _run_listener_until(dsn, handed, lambda: None, lambda: bool(handed))

    assert [h["id"] for h in handed] == [msg_id]
