"""What gets typed into an agent's session -- no database or tmux needed."""
from __future__ import annotations

from datetime import datetime, timezone

from orchestration.session.tmux import format_item

T = datetime(2026, 10, 5, 12, 0, 0, tzinfo=timezone.utc)


def _message(**overrides):
    base = {
        "kind": "message", "id": 42, "sender": "manager", "recipient": "todo", "topic": None,
        "is_broadcast": False, "content": "record this task", "in_reply_to": None,
        "thread_id": None, "reply_by": None, "new_thread": False,
    }
    return {**base, **overrides}


def test_message_starts_with_the_prefix_agents_are_told_to_look_for():
    assert format_item(_message()) == "[board message 42 from manager] record this task"


def test_header_carries_what_the_agent_needs_to_reply_and_thread():
    text = format_item(_message(topic="results", in_reply_to=7, thread_id="run-12"))
    assert text.startswith("[board message 42 from manager on #results, reply to 7, thread run-12] ")


def test_deadline_and_new_thread_are_separate_board_notes():
    lines = format_item(_message(reply_by=T, new_thread=True)).splitlines()
    assert len(lines) == 3
    assert lines[1].startswith("[board note: the sender wants a reply by ")
    assert "/compact or /clear" in lines[2]


def test_reply_timeout_note_names_the_message_and_recipient():
    text = format_item({"kind": "reply_timeout", "id": 42, "recipient": "todo", "topic": None})
    assert text.startswith("[board note: no reply arrived by the deadline for your message 42 to todo]")
