"""Formatting tests for the board view -- no database needed."""
from __future__ import annotations

from datetime import datetime, timezone

from orchestration.watch import board

T = datetime(2026, 10, 5, 12, 0, 0, tzinfo=timezone.utc)


def _message(**overrides):
    base = {
        "id": 42, "sender": "evaluator", "recipient": "monitor", "topic": None,
        "is_broadcast": False, "content": "run finished", "in_reply_to": None,
        "thread_id": None, "reply_by": None, "expires_at": None, "created_at": T,
        "recipients": ["monitor"],
    }
    return {**base, **overrides}


def test_direct_message_shows_sender_recipient_and_content():
    out = board.format_message(_message())
    assert "42" in out and "evaluator -> monitor" in out and "run finished" in out
    assert "to:" not in out  # the recipient is already in the header


def test_topic_message_lists_who_actually_got_it():
    out = board.format_message(
        _message(recipient=None, topic="eval-results", recipients=["dataset", "monitor"])
    )
    assert "evaluator -> #eval-results" in out
    assert "to: dataset, monitor" in out


def test_message_nobody_received_is_called_out():
    out = board.format_message(_message(recipient=None, topic="eval-results", recipients=[]))
    assert "delivered to nobody" in out


def test_wait_or_not_choices_and_thread_are_visible():
    out = board.format_message(_message(thread_id="t1", in_reply_to=7, reply_by=T, expires_at=T))
    assert "reply to 7" in out and "thread t1" in out
    assert "wants reply by" in out and "expires" in out


def test_long_multiline_content_is_one_short_line():
    out = board.format_message(_message(content="first\nsecond " + "x" * 500))
    body = out.splitlines()[1]
    assert "first second" in body and len(body.strip()) <= board.CONTENT_WIDTH


def test_agent_online_and_offline():
    online = {"label": "monitor", "online": True, "runner": "codex", "last_seen_at": T}
    never = {"label": "dataset", "online": False, "runner": None, "last_seen_at": None}
    assert "monitor" in board.format_agent(online) and "online (codex)" in board.format_agent(online)
    assert "offline, never seen" in board.format_agent(never)


def test_events():
    assert "handed to monitor" in board.format_event(
        {"kind": "delivered", "at": T, "message_id": 42, "agent_id": "monitor"})
    assert "monitor finished it" in board.format_event(
        {"kind": "acked", "at": T, "message_id": 42, "agent_id": "monitor"})
    assert "no reply by the deadline" in board.format_event(
        {"kind": "reply_timeout", "at": T, "message_id": 42, "agent_id": "evaluator"})
