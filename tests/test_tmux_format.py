"""What gets typed into an agent's session -- no database or tmux needed."""
from __future__ import annotations

from datetime import datetime, timezone

from orchestration.session.tmux import format_item, has_typed_text, idle_screen

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


# The input box as `tmux capture-pane -e -p` shows it (Claude Code 2.1).
EMPTY_BOX = '\x1b[39m\u276f\u00a0\x1b[2mTry\x1b[0m \x1b[2m"create\x1b[0m \x1b[2ma\x1b[0m \x1b[2mutil..."\x1b[0m'
TYPED_BOX = "\x1b[39m\u276f\u00a0hello there"
SENT_LINE = "\x1b[38;5;246m\x1b[48;5;237m\u276f \x1b[38;5;231mhello there\x1b[39m"


def test_an_empty_input_box_is_free_even_with_its_greyed_suggestion():
    assert not has_typed_text(EMPTY_BOX)
    assert not has_typed_text("\x1b[39m\u276f\u00a0")
    assert not has_typed_text("no input box on screen at all")


def test_text_the_human_typed_holds_board_messages_back():
    assert has_typed_text(TYPED_BOX)
    assert has_typed_text("\x1b[39m\u276f\u00a0\x1b[2m\x1b[0mhi")


def test_only_the_input_box_counts_not_lines_already_sent():
    assert not has_typed_text(SENT_LINE + "\n\n" + EMPTY_BOX)
    assert has_typed_text(SENT_LINE + "\n\n" + TYPED_BOX)


def test_a_waiting_agent_has_an_idle_screen_down_to_its_input_box():
    screen = "earlier output\n" + EMPTY_BOX + "\n---\n  status line 12:01"
    assert idle_screen(screen) == "earlier output\n" + EMPTY_BOX
    # The status line below the box may tick; that does not count as change.
    assert idle_screen(screen.replace("12:01", "12:02")) == idle_screen(screen)


def test_a_working_agent_or_an_open_dialog_is_not_idle():
    assert idle_screen("\u2722 Thinking\u2026 (12s \u00b7 thinking)\n" + EMPTY_BOX) is None
    assert idle_screen("\u2722 Running\u2026 (1m 4s)\n" + EMPTY_BOX) is None
    assert idle_screen(" Do you want to proceed?\n \u276f 1. Yes\n   2. No") is None


# The same, as Codex (0.157) draws it.
CODEX_EMPTY_BOX = "\x1b[1m›\x1b[0m \x1b[2mAsk Codex to do anything\x1b[0m"
CODEX_TYPED_BOX = "\x1b[1m›\x1b[0m hello"
CODEX_SENT_LINE = "\x1b[1;2m› \x1b[0mReply with just the word hi"
CODEX_WORKING = ("\x1b[1m\x1b[38;2;128;128;128m•\x1b[0m \x1b[2mWorking\x1b[0m "
                 "\x1b[2m(1s • \x1b[0;1mesc\x1b[0;2m to interrupt)\x1b[0m")
CODEX_TRUST_MENU = "\x1b[1;7m› 1. Trust and continue\n\x1b[0m  2. Quit"


def test_codex_input_box_empty_or_typed():
    assert not has_typed_text(CODEX_EMPTY_BOX, "codex")
    assert has_typed_text(CODEX_TYPED_BOX, "codex")
    assert not has_typed_text(CODEX_SENT_LINE + "\n" + CODEX_EMPTY_BOX, "codex")


def test_codex_is_idle_only_with_its_input_box_up_and_nothing_working():
    screen = CODEX_SENT_LINE + "\n• hi\n" + CODEX_EMPTY_BOX + "\n  gpt · ~/work"
    assert idle_screen(screen, "codex") == CODEX_SENT_LINE + "\n• hi\n" + CODEX_EMPTY_BOX
    assert idle_screen(CODEX_SENT_LINE + "\n" + CODEX_WORKING + "\n" + CODEX_EMPTY_BOX, "codex") is None
    # A menu (here: "trust this folder?") is never taken for the input box.
    assert idle_screen(CODEX_TRUST_MENU, "codex") is None
    assert idle_screen(CODEX_SENT_LINE, "codex") is None


def test_the_new_thread_note_names_the_runners_own_command():
    assert "/clear" in format_item(_message(new_thread=True))
    assert "/new" in format_item(_message(new_thread=True), "codex")


def test_codex_input_box_with_a_background_colour():
    # As Codex draws it once a terminal is attached: a background after the sign.
    bg = "\x1b[48;2;49;52;57m"
    empty = f"\x1b[1m\u203a\x1b[0m{bg} \x1b[2mAsk Codex to do anything\x1b[0m{bg}   "
    typed = f"\x1b[1m\u203a\x1b[0m{bg} hello\x1b[0m{bg}   "
    assert idle_screen(empty, "codex") == empty
    assert not has_typed_text(empty, "codex")
    assert has_typed_text(typed, "codex")
    # The 2 inside a colour (48;2;r;g;b) is not "dim".
    assert has_typed_text(f"\x1b[39m\u276f\u00a0{bg}hello")
