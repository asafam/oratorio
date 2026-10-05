"""Workspaces: two teams on one board must never see or reach each other,
even when their agents have the same names.

Requires a real Postgres at ORCH_BOARD_DSN with the schema applied.
"""
from __future__ import annotations

import os

import psycopg
import pytest

from orchestration.board_core import auth, messages, registry
from orchestration.listener import channels

pytestmark = pytest.mark.skipif(
    not os.environ.get("ORCH_BOARD_DSN"),
    reason="ORCH_BOARD_DSN not set -- workspace tests need a real Postgres",
)

A, B = "toy-ws-a", "toy-ws-b"


def _agent(conn, workspace: str, name: str, topics=(), is_auditor=False) -> str:
    return registry.upsert_agent(
        conn, name=name, workspace=workspace, role_doc_path=f"orchestration/roles/{name}.md",
        role_version="test", brief="toy", peers=[], topics=list(topics),
        auth_token_hash=auth.hash_token(auth.generate_token()), is_auditor=is_auditor,
    )


@pytest.fixture
def conn():
    c = psycopg.connect(os.environ["ORCH_BOARD_DSN"], autocommit=True)
    c.execute("INSERT INTO board.topic (topic) VALUES ('test-topic') ON CONFLICT DO NOTHING")
    yield c
    c.execute("DELETE FROM board.message WHERE workspace LIKE 'toy-ws-%' AND in_reply_to IS NOT NULL")
    c.execute("DELETE FROM board.message WHERE workspace LIKE 'toy-ws-%'")
    c.execute("DELETE FROM board.agent WHERE workspace LIKE 'toy-ws-%'")
    c.execute("DELETE FROM board.workspace WHERE name LIKE 'toy-ws-%'")
    c.close()


@pytest.fixture
def teams(conn):
    """The same three names in both workspaces."""
    return {
        ws: {name: _agent(conn, ws, name, topics=["test-topic"], is_auditor=(name == "manager"))
             for name in ("manager", "todo", "experiment-1")}
        for ws in (A, B)
    }


def _inbox(conn, agent_id):
    return [m["id"] for m in messages.read_messages(conn, agent_id)]


def test_same_name_in_two_workspaces_is_two_agents(teams):
    assert teams[A]["manager"] != teams[B]["manager"]


def test_direct_message_by_name_stays_in_the_senders_workspace(conn, teams):
    sent = messages.post_message(conn, teams[A]["manager"], recipient="todo", content="for A's todo")
    assert sent["delivered_to"] == ["todo"]
    assert _inbox(conn, teams[A]["todo"]) == [sent["message_id"]]
    assert _inbox(conn, teams[B]["todo"]) == []
    # The reader sees a plain name, not an internal key.
    assert messages.read_messages(conn, teams[A]["todo"])[0]["sender"] == "manager"


def test_a_name_that_only_exists_elsewhere_cannot_be_addressed(conn, teams):
    _agent(conn, B, "only-in-b")
    with pytest.raises(messages.RoutingError):
        messages.post_message(conn, teams[A]["manager"], recipient="only-in-b", content="x")
    # Nor by guessing the other agent's internal key.
    with pytest.raises(messages.RoutingError):
        messages.post_message(conn, teams[A]["manager"], recipient=teams[B]["todo"], content="x")


def test_topic_and_broadcast_reach_only_the_senders_workspace(conn, teams):
    topic = messages.post_message(conn, teams[A]["manager"], topic="test-topic", content="t")
    everyone = messages.post_message(conn, teams[A]["manager"], broadcast=True, content="b")
    for sent in (topic, everyone):
        assert sent["delivered_to"] == ["experiment-1", "todo"]
    for name in ("manager", "todo", "experiment-1"):
        assert _inbox(conn, teams[B][name]) == []


def test_cannot_reply_to_another_workspaces_message(conn, teams):
    theirs = messages.post_message(conn, teams[B]["manager"], recipient="todo", content="B only")
    with pytest.raises(messages.RoutingError):
        messages.post_message(
            conn, teams[A]["manager"], recipient="todo", msg_type="REPLY",
            in_reply_to=theirs["message_id"], content="sneaking in",
        )


def test_database_refuses_a_cross_workspace_message_even_from_raw_sql(conn, teams):
    """The Python layer is not the only guard: hand-written SQL that names
    another workspace's agent or message is rejected by the board itself."""
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO board.message (sender_agent_id, recipient_agent_id, content) "
            "VALUES (%s, %s, 'x')",
            (teams[A]["manager"], teams[B]["todo"]),
        )
    theirs = messages.post_message(conn, teams[B]["manager"], recipient="todo", content="B only")
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO board.message (sender_agent_id, recipient_agent_id, content, in_reply_to) "
            "VALUES (%s, %s, 'x', %s)",
            (teams[A]["manager"], teams[A]["todo"], theirs["message_id"]),
        )


def test_auditor_and_agent_list_see_only_their_own_workspace(conn, teams):
    mine = messages.post_message(conn, teams[A]["todo"], recipient="manager", content="A")
    theirs = messages.post_message(conn, teams[B]["todo"], recipient="manager", content="B")

    seen = {m["id"] for m in registry.read_all_messages(conn, teams[A]["manager"])}
    assert mine["message_id"] in seen and theirs["message_id"] not in seen

    listed = registry.list_agents(conn, workspace=A)
    assert {a["name"] for a in listed} == {"manager", "todo", "experiment-1"}
    assert {a["workspace"] for a in listed} == {A}


def test_topic_backfill_does_not_pull_in_another_workspaces_history(conn, teams):
    theirs = messages.post_message(conn, teams[B]["manager"], topic="test-topic", content="B history")
    late = _agent(conn, A, "latecomer")
    registry.subscribe(conn, late, "test-topic", backfill=True)
    assert theirs["message_id"] not in _inbox(conn, late)


def test_listen_channels_are_separate_per_workspace(conn, teams):
    """A post in one workspace must not even wake a listener in another."""
    a = channels.agent_channels(conn, teams[A]["todo"])
    b = channels.agent_channels(conn, teams[B]["todo"])
    assert a & b == {channels.TOPOLOGY_CHANNEL}
