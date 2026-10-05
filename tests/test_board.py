"""Phase 0 tests: prove the durable message-board schema/logic works before
wiring in any MCP transport, webhook, or real LLM process.

Requires a real Postgres reachable at ORCH_BOARD_DSN (schema already
applied -- every numbered file under orchestration/schema/, in order).
Skipped entirely if that's not set, so this never blocks the unit-test
suite that runs without any API key/service dependency.
"""
from __future__ import annotations

import os
import time

import psycopg
import pytest

from orchestration.board_core import auth, messages, registry

pytestmark = pytest.mark.skipif(
    not os.environ.get("ORCH_BOARD_DSN"),
    reason="ORCH_BOARD_DSN not set -- orchestration board tests need a real Postgres",
)


def _conn():
    return psycopg.connect(os.environ["ORCH_BOARD_DSN"], autocommit=True)


def _make_agent(conn, agent_id: str, topics: list[str] | None = None, is_auditor: bool = False):
    token = auth.generate_token()
    registry.upsert_agent(
        conn,
        agent_id=agent_id,
        role_doc_path=f"orchestration/roles/{agent_id}.md",
        role_version="test",
        brief=f"toy agent {agent_id}",
        peers=[],
        topics=topics or [],
        auth_token_hash=auth.hash_token(token),
        is_auditor=is_auditor,
    )
    return token


@pytest.fixture
def conn():
    c = _conn()
    c.execute(
        "INSERT INTO board.topic (topic, description) VALUES ('test-topic', 'test fixture topic') "
        "ON CONFLICT DO NOTHING"
    )
    yield c
    # Clean up everything this test suite creates -- toy agent ids are
    # namespaced with a "toy-" prefix specifically so cleanup can't touch
    # real role rows synced by orchestration/roles/sync_roles.py. Messages
    # (and their message_delivery rows, via ON DELETE CASCADE from message)
    # must go first -- board.message.sender/recipient reference board.agent
    # WITHOUT cascade, deliberately: messages are an append-only audit log
    # that should never silently vanish just because an agent row changes,
    # only this test suite's own toy rows are torn down explicitly.
    c.execute(
        "DELETE FROM board.message WHERE sender_agent_id LIKE 'toy-%' "
        "OR recipient_agent_id LIKE 'toy-%'"
    )
    c.execute("DELETE FROM board.agent WHERE agent_id LIKE 'toy-%'")
    c.close()


def test_topic_fanout_reaches_only_subscribers(conn):
    _make_agent(conn, "toy-a", topics=["test-topic"])
    _make_agent(conn, "toy-b", topics=["test-topic"])
    _make_agent(conn, "toy-c", topics=[])  # registered, NOT subscribed

    result = messages.post_message(
        conn, "toy-a", topic="test-topic", content="hello subscribers"
    )
    assert set(result["delivered_to"]) == {"toy-b"}  # sender excluded, toy-c not subscribed

    b_inbox = messages.read_messages(conn, "toy-b")
    assert any(m["id"] == result["message_id"] for m in b_inbox)

    c_inbox = messages.read_messages(conn, "toy-c")
    assert not any(m["id"] == result["message_id"] for m in c_inbox)


def test_pending_tracked_independently_per_agent(conn):
    _make_agent(conn, "toy-a", topics=["test-topic"])
    _make_agent(conn, "toy-b", topics=["test-topic"])
    _make_agent(conn, "toy-c", topics=["test-topic"])

    result = messages.post_message(conn, "toy-a", topic="test-topic", content="msg")
    messages.ack_message(conn, "toy-b", result["message_id"])

    assert not any(
        m["id"] == result["message_id"]
        for m in messages.read_messages(conn, "toy-b")
    )
    assert any(
        m["id"] == result["message_id"]
        for m in messages.read_messages(conn, "toy-c")
    )


def test_direct_reply_correlates_by_id_not_peer_identity(conn):
    """Regression test for a classic correlation bug: two concurrent
    direct requests from the same sender to the same recipient, replied to
    OUT OF ORDER, must each resolve to the correct original request via
    in_reply_to -- never by "a reply just arrived from this peer"."""
    _make_agent(conn, "toy-a")
    _make_agent(conn, "toy-b")

    req1 = messages.post_message(
        conn, "toy-a", recipient="toy-b", content="question 1", expects_reply=True
    )
    req2 = messages.post_message(
        conn, "toy-a", recipient="toy-b", content="question 2", expects_reply=True
    )
    assert req1["message_id"] != req2["message_id"]

    # Reply out of order: answer req2 first, then req1.
    reply2 = messages.post_message(
        conn, "toy-b", recipient="toy-a", msg_type="REPLY",
        in_reply_to=req2["message_id"], content="answer 2",
    )
    reply1 = messages.post_message(
        conn, "toy-b", recipient="toy-a", msg_type="REPLY",
        in_reply_to=req1["message_id"], content="answer 1",
    )

    a_inbox = {m["id"]: m for m in messages.read_messages(conn, "toy-a")}
    assert a_inbox[reply1["message_id"]]["in_reply_to"] == req1["message_id"]
    assert a_inbox[reply2["message_id"]]["in_reply_to"] == req2["message_id"]


def test_overseer_full_visibility_without_delivery_rows(conn):
    _make_agent(conn, "toy-a", topics=["test-topic"])
    _make_agent(conn, "toy-overseer", is_auditor=True)  # NOT subscribed to test-topic

    result = messages.post_message(conn, "toy-a", topic="test-topic", content="visible to all")

    audit = registry.read_all_messages(conn, "toy-overseer")
    assert any(m["id"] == result["message_id"] for m in audit)

    # No message_delivery row should exist for the overseer -- auditing
    # isn't "consuming".
    row = conn.execute(
        "SELECT 1 FROM board.message_delivery WHERE agent_id = 'toy-overseer' AND message_id = %s",
        (result["message_id"],),
    ).fetchone()
    assert row is None

    with pytest.raises(registry.AuthorizationError):
        registry.read_all_messages(conn, "toy-a")  # not an auditor


def test_notify_is_not_delivery(conn):
    """The single most important regression test in this subsystem: a
    dropped/never-started LISTEN connection must never lose a message.
    Durability comes from message_delivery rows written in the same
    transaction as the insert, NOT from a listener being connected when
    NOTIFY fires."""
    _make_agent(conn, "toy-a", topics=["test-topic"])
    _make_agent(conn, "toy-b", topics=["test-topic"])

    # No LISTEN connection is ever opened in this test -- simulates the
    # headless-agent-is-down-most-of-the-time reality directly.
    ids = [
        messages.post_message(conn, "toy-a", topic="test-topic", content=f"msg {i}")["message_id"]
        for i in range(3)
    ]

    inbox_ids = {m["id"] for m in messages.read_messages(conn, "toy-b")}
    assert set(ids) <= inbox_ids


def test_depth_remaining_stops_reply_cascade(conn):
    _make_agent(conn, "toy-a")
    _make_agent(conn, "toy-b")

    result = messages.post_message(conn, "toy-a", recipient="toy-b", content="start")
    parent_depth = conn.execute(
        "SELECT depth_remaining FROM board.message WHERE id = %s", (result["message_id"],)
    ).fetchone()[0]
    assert parent_depth == messages.DEFAULT_DEPTH

    msg_id = result["message_id"]
    # Walk the depth down to 0 via a chain of replies.
    for _ in range(parent_depth):
        reply = messages.post_message(
            conn, "toy-b", recipient="toy-a", msg_type="REPLY",
            in_reply_to=msg_id, content="reply",
        )
        msg_id = reply["message_id"]

    depth = conn.execute(
        "SELECT depth_remaining FROM board.message WHERE id = %s", (msg_id,)
    ).fetchone()[0]
    assert depth == 0

    # One more reply is inserted (that's allowed -- it's the CALLER's own
    # message) but the fan-out trigger must refuse to deliver it further.
    final = messages.post_message(
        conn, "toy-a", recipient="toy-b", msg_type="REPLY", in_reply_to=msg_id, content="too deep"
    )
    assert final["delivered_to"] == []


def test_reading_does_not_consume_only_ack_does(conn):
    """A session that reads a message and is then cleared (or crashes) must
    not lose it -- only an explicit ack takes it off the pending list."""
    _make_agent(conn, "toy-a")
    _make_agent(conn, "toy-b")
    msg_id = messages.post_message(conn, "toy-a", recipient="toy-b", content="work")["message_id"]

    for _ in range(2):
        assert [m["id"] for m in messages.read_messages(conn, "toy-b")] == [msg_id]
    assert not messages.is_caught_up(conn, "toy-b")

    assert messages.ack_message(conn, "toy-b", msg_id)
    assert messages.read_messages(conn, "toy-b") == []
    assert messages.is_caught_up(conn, "toy-b")


def test_handed_over_but_unacked_is_requeued_for_a_new_session(conn):
    _make_agent(conn, "toy-a")
    _make_agent(conn, "toy-b")
    msg_id = messages.post_message(conn, "toy-a", recipient="toy-b", content="work")["message_id"]

    assert [m["id"] for m in messages.undelivered(conn, "toy-b")] == [msg_id]
    messages.mark_delivered(conn, "toy-b", [msg_id])
    assert messages.undelivered(conn, "toy-b") == []  # handed over once, not again

    assert messages.requeue_unacked(conn, "toy-b") == 1  # the session started over
    assert [m["id"] for m in messages.undelivered(conn, "toy-b")] == [msg_id]

    messages.mark_delivered(conn, "toy-b", [msg_id])
    messages.ack_message(conn, "toy-b", msg_id)
    assert messages.requeue_unacked(conn, "toy-b") == 0  # done stays done


def test_expired_message_is_not_pending(conn):
    """The sender's "don't wait for a recipient that is down" choice."""
    _make_agent(conn, "toy-a")
    _make_agent(conn, "toy-b")
    waits = messages.post_message(conn, "toy-a", recipient="toy-b", content="whenever")
    drops = messages.post_message(
        conn, "toy-a", recipient="toy-b", content="only matters now", expires_in_seconds=1
    )
    assert {m["id"] for m in messages.read_messages(conn, "toy-b")} == {
        waits["message_id"], drops["message_id"]
    }

    time.sleep(1.5)
    assert [m["id"] for m in messages.read_messages(conn, "toy-b")] == [waits["message_id"]]
    assert [m["id"] for m in messages.undelivered(conn, "toy-b")] == [waits["message_id"]]


def test_reply_deadline_is_reported_once_and_only_if_unanswered(conn):
    _make_agent(conn, "toy-a")
    _make_agent(conn, "toy-b")
    answered = messages.post_message(
        conn, "toy-a", recipient="toy-b", content="q1", reply_within_seconds=1
    )
    unanswered = messages.post_message(
        conn, "toy-a", recipient="toy-b", content="q2", reply_within_seconds=1
    )
    messages.post_message(
        conn, "toy-b", recipient="toy-a", msg_type="REPLY",
        in_reply_to=answered["message_id"], content="a1",
    )
    assert messages.claim_overdue_requests(conn, "toy-a") == []  # deadline not reached yet

    time.sleep(1.5)
    overdue = messages.claim_overdue_requests(conn, "toy-a")
    assert [r["id"] for r in overdue] == [unanswered["message_id"]]
    assert messages.claim_overdue_requests(conn, "toy-a") == []  # told once, not every sweep


def test_reply_inherits_thread_id(conn):
    _make_agent(conn, "toy-a")
    _make_agent(conn, "toy-b")
    req = messages.post_message(
        conn, "toy-a", recipient="toy-b", content="start", thread_id="toy-thread-1"
    )
    messages.post_message(
        conn, "toy-b", recipient="toy-a", msg_type="REPLY",
        in_reply_to=req["message_id"], content="reply",
    )
    assert [m["thread_id"] for m in messages.read_messages(conn, "toy-a")] == ["toy-thread-1"]


def test_register_marks_agent_online_and_sets_subscriptions(conn):
    _make_agent(conn, "toy-a", topics=[])
    before = {a["agent_id"]: a for a in registry.list_agents(conn)}["toy-a"]
    assert before["online"] is False and before["runner"] is None

    registry.register(conn, "toy-a", runner="codex", topics=["test-topic"])

    after = {a["agent_id"]: a for a in registry.list_agents(conn)}["toy-a"]
    assert after["online"] is True and after["runner"] == "codex"
    assert after["topics"] == ["test-topic"]

    _make_agent(conn, "toy-b")
    result = messages.post_message(conn, "toy-b", topic="test-topic", content="hi")
    assert result["delivered_to"] == ["toy-a"]
