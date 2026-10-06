"""Hand board messages to a live agent session running in a tmux pane, by
typing them in -- the same way a human would. Works for any terminal
agent (Claude Code, Codex, ...), since all it needs is a terminal.

The one hard rule: never type while the agent is mid-turn. "Idle" is a
flag file kept by the agent's own hooks (see session/up.py): present
while the session is waiting for input, removed the moment a prompt is
submitted. A second marker, `<flag>.cleared`, appears when the human runs
/clear, so the listener knows to hand open messages in again.

The flag can go missing for good: a turn that ends without the agent's
Stop hook running (interrupted with Esc, or failed -- say, a request cut
off when the laptop slept) never puts it back, and the listener would
wait forever. So when the flag has been missing a while, the listener
looks at the screen: an empty-or-typed input box, no "working" spinner,
and nothing changing for STUCK_AFTER_SECONDS means the agent is idle.

Codex has no such hooks without asking you to trust them, so for a Codex
session the screen always has the last word: the flag (put back by
Codex's `notify` when a turn ends) only counts while the screen agrees
that the input box is up and nothing is working.

The second rule: never type over the human. While there is text in the
session's input box, board messages wait; whoever is looking at that
pane gets a short note that something is waiting. What is left: a
message that lands in the instant between two keystrokes of the first
word can still get in ahead of it.
"""
from __future__ import annotations

import logging
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

# How long the session gets to take in a paste before Enter is pressed.
PASTE_SETTLE_SECONDS = 0.4
# While the human's text holds a message back: how often to remind them.
NUDGE_EVERY_SECONDS = 30
# How long a session must sit still, flag or no flag, to count as idle.
STUCK_AFTER_SECONDS = 30

logger = logging.getLogger("orchestration.listener")
# What a working session shows above its input box: "Thinking… (12s", "(1m 4s".
_SPINNER = re.compile(r"\u2026 \((\d+[hm] )*\d+s")

# Claude Code's input box starts with this (a no-break space, unlike the
# plain space after the prompt sign on lines already sent).
INPUT_PROMPT = re.compile("\u276f\u00a0")
_SGR = re.compile(r"\x1b\[([0-9;]*)m")

# The same two things for Codex (0.157). Its input box starts with a bold
# `›` (then, with some terminals attached, a background colour) and a
# space; lines already sent start with a dim one, and a menu's chosen row
# (the "trust this folder?" one) with a reversed one -- so the colour
# codes, not the sign, tell them apart. Working, it shows
# "• Working (12s • esc to interrupt)" above the box.
CODEX_INPUT_PROMPT = re.compile("\x1b\\[1m\u203a(?:\x1b\\[[0-9;]*m)* ")
_CODEX_SPINNER = re.compile(r"Working \((\d+[hm] )*\d+s")

SCREENS = {"claude": (INPUT_PROMPT, _SPINNER), "codex": (CODEX_INPUT_PROMPT, _CODEX_SPINNER)}


def idle_screen(screen: str, runner: str = "claude") -> str | None:
    """If the screen looks like an agent waiting for input -- its input box
    is there and nothing is working -- the part that matters for telling
    whether it changes (everything down to the input box; below it is the
    status line, which may tick on its own). Otherwise None."""
    prompt, spinner = SCREENS[runner]
    lines = screen.splitlines()
    for i in range(len(lines) - 1, -1, -1):
        if prompt.search(lines[i]):
            above = "\n".join(lines[: i + 1])
            return None if spinner.search(_SGR.sub("", above)) else above  # colour codes split it up
    return None  # no input box: a question or permission dialog is up, or it is busy


def has_typed_text(screen: str, runner: str = "claude") -> bool:
    """Is there text of the human's in the input box? `screen` is the pane
    as `tmux capture-pane -e -p` gives it (with colour codes). The greyed
    suggestion an empty box shows is dim text, and does not count."""
    prompt, _ = SCREENS[runner]
    for line in reversed(screen.splitlines()):
        found = prompt.search(line)
        if not found:
            continue
        box = line[found.end():]
        dim, at = False, 0
        for code in _SGR.finditer(box):
            if not dim and box[at:code.start()].strip():
                return True
            numbers = code.group(1).split(";")
            while numbers:
                n = numbers.pop(0)
                if n in ("38", "48", "58") and numbers:  # a colour: its own numbers follow, skip them
                    del numbers[: 2 if numbers[0] == "5" else 4 if numbers[0] == "2" else 0]
                elif n == "2":
                    dim = True
                elif n in ("", "0", "22"):
                    dim = False
            at = code.end()
        return not dim and bool(box[at:].strip())
    return False


def _clock(ts: datetime) -> str:
    return ts.astimezone().strftime("%H:%M")


def format_item(item: dict[str, Any], runner: str = "claude") -> str:
    """The exact text typed into the session. Every board line starts with
    `[board` so the agent can tell it from the human typing."""
    if item["kind"] == "reply_timeout":
        target = item["recipient"] or (f"#{item['topic']}" if item["topic"] else "everyone")
        return (
            f"[board note: no reply arrived by the deadline for your message {item['id']} "
            f"to {target}] Decide what to do next: ask again, ask someone else, or go on without it."
        )

    head = f"[board message {item['id']} from {item['sender']}"
    if item["topic"]:
        head += f" on #{item['topic']}"
    elif item["is_broadcast"]:
        head += " to everyone"
    if item["in_reply_to"]:
        head += f", reply to {item['in_reply_to']}"
    if item["thread_id"]:
        head += f", thread {item['thread_id']}"
    head += "]"
    lines = [f"{head} {item['content']}"]
    if item["reply_by"]:
        lines.append(f"[board note: the sender wants a reply by {_clock(item['reply_by'])}]")
    if item.get("new_thread"):
        lines.append(
            "[board note: this starts a different thread than your last message -- "
            f"if the earlier work is finished, this is a good moment for /compact or "
            f"{'/new' if runner == 'codex' else '/clear'}]"
        )
    return "\n".join(lines)


class TmuxSession:
    def __init__(self, pane: str, idle_flag: Path, runner: str = "claude"):
        self.pane = pane
        self.runner = runner if runner in SCREENS else "claude"
        self.idle_flag = idle_flag
        self.cleared_marker = Path(str(idle_flag) + ".cleared")
        self._last_nudge = float("-inf")
        self._last_hand = float("-inf")
        self._still: tuple[str, float] | None = None  # (idle-looking screen, since when)

    def ready(self) -> bool:
        if self.idle_flag.exists():
            self._still = None
            if self.runner == "codex":  # the flag alone is not enough: see the top of this file
                screen = self.screen()
                return (screen is not None and idle_screen(screen, self.runner) is not None
                        and not has_typed_text(screen, self.runner))
            return not self.human_is_typing()
        return self._stuck_but_idle()

    def screen(self) -> str | None:
        shown = subprocess.run(["tmux", "capture-pane", "-e", "-p", "-t", self.pane],
                               capture_output=True, text=True)
        return shown.stdout if shown.returncode == 0 else None

    def human_is_typing(self) -> bool:
        screen = self.screen()
        return screen is not None and has_typed_text(screen, self.runner)

    def _stuck_but_idle(self) -> bool:
        """No idle flag: the agent is working -- or its last turn ended
        without saying so. Tell the two apart by the screen standing still,
        idle-looking, for STUCK_AFTER_SECONDS; then put the flag back."""
        screen = self.screen()
        key = idle_screen(screen, self.runner) if screen is not None else None
        now = time.monotonic()
        if key is None:
            self._still = None
            return False
        if self._still is None or self._still[0] != key:
            self._still = (key, now)
            return False
        if now - self._still[1] < STUCK_AFTER_SECONDS or now - self._last_hand < STUCK_AFTER_SECONDS:
            return False
        logger.warning("pane %s has sat idle for %ds without its idle flag (a turn that was "
                       "interrupted or failed) -- treating it as idle", self.pane, STUCK_AFTER_SECONDS)
        self.idle_flag.touch()
        self._still = None
        return not has_typed_text(screen, self.runner)

    def nudge_if_held(self, name: str) -> None:
        """Something is waiting for this session and only the human's
        half-typed text is in the way: tell whoever is looking at it, now
        and then, so the message is not held back unnoticed."""
        if not self.idle_flag.exists() or not self.human_is_typing():
            return
        if time.monotonic() - self._last_nudge < NUDGE_EVERY_SECONDS:
            return
        self._last_nudge = time.monotonic()
        window = subprocess.run(["tmux", "display-message", "-p", "-t", self.pane, "#{window_id}"],
                                capture_output=True, text=True).stdout.strip()
        clients = subprocess.run(["tmux", "list-clients", "-F", "#{client_name}\t#{window_id}"],
                                 capture_output=True, text=True).stdout.splitlines()
        for client, _, shown in (line.partition("\t") for line in clients):
            if shown == window:
                subprocess.run(["tmux", "display-message", "-c", client, "-d", "4000",
                                f"A board message is waiting for {name}: send or clear your text to let it in."])

    def was_cleared(self) -> bool:
        """True once per /clear."""
        if self.cleared_marker.exists():
            self.cleared_marker.unlink(missing_ok=True)
            return True
        return False

    def hand(self, item: dict[str, Any]) -> None:
        # Drop the flag ourselves rather than waiting for the hook, so a
        # second message can't slip in before the session reports busy.
        self.idle_flag.unlink(missing_ok=True)
        self._last_hand = time.monotonic()
        self._still = None
        buffer = f"oratorio-{self.pane.lstrip('%')}"
        # A bracketed paste (-p) arrives as one block, so newlines in the
        # message don't submit it early; Enter is sent separately after.
        subprocess.run(["tmux", "load-buffer", "-b", buffer, "-"],
                       input=format_item(item, self.runner).encode(), check=True)
        subprocess.run(["tmux", "paste-buffer", "-p", "-d", "-b", buffer, "-t", self.pane], check=True)
        time.sleep(PASTE_SETTLE_SECONDS)
        subprocess.run(["tmux", "send-keys", "-t", self.pane, "Enter"], check=True)
