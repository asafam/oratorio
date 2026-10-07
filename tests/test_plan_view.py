"""The plan view reads TODO.md -- no database or tmux needed."""
from __future__ import annotations

from datetime import datetime

from orchestration.watch.plan import parse, render

TODO = """# Plan: docs/research/plan.md

## Waiting for you
- [?] T14 Use bAbI or CLUTRR? | manager | asked 2026-10-07 09:10

## In progress
- [>] T12 Baseline on bAbI | experiment-1 | Q1 | since 2026-10-07 09:10
- [>] T15 Fresh task | experiment-2 | Q1 | since 2026-10-07 11:50

## Pending
- [ ] T13 Ablation | Q2

## Done
- [x] T9 Lift bAbI | holds | 2026-10-06

## Budget
- [ ] not a task: other sections are not read
"""
NOW = datetime(2026, 10, 7, 13, 0)


def test_tasks_are_read_by_section():
    plan = parse(TODO)
    assert plan["plan"] == "docs/research/plan.md"
    assert [len(plan["tasks"][s]) for s in ("waiting", "progress", "review", "pending", "done")] == [1, 2, 0, 1, 1]
    assert plan["tasks"]["progress"][0]["fields"][:2] == ["T12 Baseline on bAbI", "experiment-1"]
    assert plan["tasks"]["progress"][0]["when"] == datetime(2026, 10, 7, 9, 10)


def test_old_work_in_progress_is_flagged(tmp_path):
    todo = tmp_path / "TODO.md"
    todo.write_text(TODO)
    screen = render(todo, "perpetua", now=NOW).splitlines()
    assert any("T12" in line and "no news" in line for line in screen)  # 3h50m old; T15 1h10m
    assert any("T15" in line and "no news" not in line for line in screen)
    assert not any("not a task" in line for line in screen)


def test_long_paragraph_lines_are_called_out(tmp_path):
    todo = tmp_path / "TODO.md"
    todo.write_text("## In progress\n- [>] T1 " + "word " * 100 + "\n")
    assert "long paragraphs" in render(todo, "x", now=NOW)


def test_no_file_yet(tmp_path):
    assert "No TODO.md yet" in render(tmp_path / "TODO.md", "x")
