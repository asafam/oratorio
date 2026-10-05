"""Hand board messages to a live agent session running in a tmux pane, by
typing them in -- the same way a human would. Works for any terminal
agent (Claude Code, Codex, ...), since all it needs is a terminal.

The one hard rule: never type while the agent is mid-turn. "Idle" is a
flag file kept by the agent's own hooks (see session/up.py): present
while the session is waiting for input, removed the moment a prompt is
submitted. A second marker, `<flag>.cleared`, appears when the human runs
/clear, so the listener knows to hand open messages in again.

Known limit: if the human has half-typed text sitting in the input box of
an idle pane, a board message is typed on top of it.
"""
from __future__ import annotations

import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

# How long the session gets to take in a paste before Enter is pressed.
PASTE_SETTLE_SECONDS = 0.4


def _clock(ts: datetime) -> str:
    return ts.astimezone().strftime("%H:%M")


def format_item(item: dict[str, Any]) -> str:
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
            "if the earlier work is finished, this is a good moment for /compact or /clear]"
        )
    return "\n".join(lines)


class TmuxSession:
    def __init__(self, pane: str, idle_flag: Path):
        self.pane = pane
        self.idle_flag = idle_flag
        self.cleared_marker = Path(str(idle_flag) + ".cleared")

    def ready(self) -> bool:
        return self.idle_flag.exists()

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
        buffer = f"oratorio-{self.pane.lstrip('%')}"
        # A bracketed paste (-p) arrives as one block, so newlines in the
        # message don't submit it early; Enter is sent separately after.
        subprocess.run(["tmux", "load-buffer", "-b", buffer, "-"],
                       input=format_item(item).encode(), check=True)
        subprocess.run(["tmux", "paste-buffer", "-p", "-d", "-b", buffer, "-t", self.pane], check=True)
        time.sleep(PASTE_SETTLE_SECONDS)
        subprocess.run(["tmux", "send-keys", "-t", self.pane, "Enter"], check=True)
