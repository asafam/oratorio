#!/usr/bin/env python3
"""The plan view: what has been done, what is being done, and what is
still to do, read from the team's TODO.md. Leave it running in its own
terminal pane; it redraws when the file changes.

It only reads the file -- plain code, no model, so it costs no tokens --
and it does not need the board. The format it reads (the manager or lead
keeps the file in it; see their role files):

    # Plan: docs/research/plan.md

    ## Waiting for you
    - [?] T14 Use bAbI or CLUTRR for the long-chain test? | manager | asked 2026-10-07 09:10

    ## In progress
    - [>] T12 Baseline on bAbI | experiment-1 | Q1 | since 2026-10-07 09:10

    ## In review
    - [~] T11 Lift qa2 into events | reviewer | Q1 | since 2026-10-07 08:00

    ## Pending
    - [ ] T13 Ablation without the noise filter | Q2

    ## Done
    - [x] T9 Lift bAbI into events | holds | 2026-10-06

    ## Dropped
    - [-] T7 Synthetic long streams | too far from Q1, see DECISIONS.md

One line per task, newest at the top of its section: an id, a short
title, then fields split by ` | `. The
last date and time on a line is when it entered its section; work that
sits in progress or in review long past that is flagged. Other sections
(notes, budget, ...) are allowed and not shown.

Usage:
    python -m orchestration.watch.plan [TODO.md] [--title NAME] [--once]
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

POLL_SECONDS = 3
# Work in progress or in review this long since its date is flagged.
STALE_HOURS = 3
# A task line longer than this is no longer a one-line summary.
LONG_LINE = 200
SHOW_PENDING = 8
SHOW_DONE = 5

# section heading (lower case) -> (short name, how many to show; None: all)
SECTIONS = {
    "waiting for you": ("waiting", None),
    "in progress": ("progress", None),
    "in review": ("review", None),
    "pending": ("pending", SHOW_PENDING),
    "done": ("done", SHOW_DONE),
    "dropped": ("dropped", 0),
}
TITLES = {"waiting": "Waiting for you", "progress": "In progress", "review": "In review",
          "pending": "Pending", "done": "Done", "dropped": "Dropped"}
TASK = re.compile(r"^\s*- \[(.)\]\s*(.*)$")
WHEN = re.compile(r"(\d{4}-\d{2}-\d{2})(?:[ T](\d{1,2}:\d{2}))?")

_COLOR = sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def parse(text: str) -> dict:
    """{"plan": path or None, "tasks": {section: [task, ...]}, "long": n}.
    A task is {"text", "fields", "when"}."""
    plan, tasks, long_lines = None, {name: [] for name, _ in SECTIONS.values()}, 0
    section = None
    for line in text.splitlines():
        if line.startswith("# ") and plan is None:
            found = re.match(r"#\s*plan:\s*(.+)", line, re.IGNORECASE)
            plan = found.group(1).strip() if found else None
        elif line.startswith("## "):
            section = SECTIONS.get(line[3:].strip().lower(), (None, 0))[0]
        elif section and (task := TASK.match(line)):
            body = task.group(2).strip()
            long_lines += len(body) > LONG_LINE
            fields = [f.strip() for f in body.split(" | ")]
            dates = WHEN.findall(body)
            when = None
            if dates:
                day, clock = dates[-1]
                try:
                    when = datetime.strptime(f"{day} {clock or '00:00'}", "%Y-%m-%d %H:%M")
                except ValueError:
                    pass
            tasks[section].append({"text": body, "fields": fields, "when": when})
    return {"plan": plan, "tasks": tasks, "long": long_lines}


def _age(when: datetime | None, now: datetime) -> str:
    if when is None:
        return ""
    minutes = int((now - when).total_seconds() // 60)
    if minutes < 60:
        return f"{max(minutes, 0)}m"
    if minutes < 48 * 60:
        return f"{minutes // 60}h"
    return f"{minutes // (24 * 60)}d"


def _fit(text: str, width: int) -> str:
    return text if len(text) <= width else text[: max(width - 1, 0)] + "…"


def render(path: Path, title: str, now: datetime | None = None, width: int = 100) -> str:
    now = now or datetime.now()
    if not path.exists():
        return (f"{_c('1', 'PLAN')}  {title}\n\nNo {path.name} yet in {path.parent}.\n"
                "The manager (or lead) writes it when it plans the first work.")
    changed = datetime.fromtimestamp(path.stat().st_mtime)
    plan = parse(path.read_text())
    tasks = plan["tasks"]
    lines = [f"{_c('1', 'PLAN')}  {title}   {path.name} changed {_age(changed, now)} ago",
             "  ·  ".join(f"{TITLES[s]} {len(t)}" for s, t in tasks.items() if t or s != "review")]
    if plan["plan"]:
        lines.append(_c("2", f"plan: {plan['plan']}"))
    if plan["long"]:
        lines.append(_c("33", f"⚠ {plan['long']} task line(s) are long paragraphs -- keep one short line per task"))
    for (heading, (section, shown)) in SECTIONS.items():
        items = tasks[section]
        if not items or shown == 0:
            continue
        items, more = (items[:shown], len(items) - shown) if shown else (items, 0)  # newest first
        colour = {"waiting": "1;35", "progress": "1;36", "review": "1;34"}.get(section, "1")
        lines += ["", _c(colour, TITLES[section].upper())]
        for task in items:
            first, rest = task["fields"][0], [f for f in task["fields"][1:] if not WHEN.search(f)]
            age = _age(task["when"], now) if section in ("waiting", "progress", "review") else ""
            stale = (section in ("progress", "review") and task["when"] is not None
                     and (now - task["when"]).total_seconds() > STALE_HOURS * 3600)
            # The title first; what is left of the width goes to the other fields.
            text = _fit(first, max(width * 3 // 5, 30))
            room = width - len(text) - len(age) - 18
            tail = "  ".join(x for x in (_fit(" · ".join(rest), max(room, 0)) if room > 8 else "", age) if x)
            line = f"  {text}" + (f"  {_c('2', tail)}" if tail else "")
            lines.append(line + (_c("33", "  ⚠ no news") if stale else ""))
        if more > 0:
            lines.append(_c("2", f"  ... and {more} more" if section != "done" else f"  ({more} earlier)"))
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Live view of the team's plan (TODO.md).")
    parser.add_argument("file", nargs="?", default="TODO.md", help="the plan file (default: TODO.md here)")
    parser.add_argument("--title", default="", help="a name to show at the top (the workspace)")
    parser.add_argument("--once", action="store_true", help="print it once and exit")
    args = parser.parse_args()
    path = Path(args.file)
    if args.once:
        print(render(path, args.title, width=shutil.get_terminal_size().columns))
        return
    shown, last_draw = None, 0.0
    try:
        while True:
            stamp = (path.stat().st_mtime if path.exists() else None, shutil.get_terminal_size())
            # Redraw when the file or the terminal changes, and each minute for the ages.
            if stamp != shown or time.monotonic() - last_draw > 60:
                screen = render(path, args.title, width=stamp[1].columns)
                sys.stdout.write("\033[H\033[2J" + screen + "\n")
                sys.stdout.flush()
                shown, last_draw = stamp, time.monotonic()
            time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    main()
