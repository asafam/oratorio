"""Turning a name the user typed into an agent -- no database or tmux needed."""
from __future__ import annotations

import pytest

from orchestration.session.up import _team_order, resolve_agent, role_files

ROLES = role_files()


def test_single_role_is_its_own_agent():
    assert resolve_agent("manager", ROLES, set()) == ("manager", "manager")


def test_multiple_role_gets_the_next_free_number():
    assert resolve_agent("experiment", ROLES, set()) == ("experiment-1", "experiment")
    assert resolve_agent("experiment", ROLES, {"experiment-1"}) == ("experiment-2", "experiment")
    # A gap left by a removed agent is reused -- it picks up that agent's waiting messages.
    assert resolve_agent("experiment", ROLES, {"experiment-2"}) == ("experiment-1", "experiment")


def test_exact_numbered_agent_is_taken_as_given():
    assert resolve_agent("experiment-7", ROLES, set()) == ("experiment-7", "experiment")


def test_unknown_names_are_refused():
    for name in ("nobody", "manager-2", "experiment-x"):
        with pytest.raises(ValueError):
            resolve_agent(name, ROLES, set())


def test_saved_team_is_ordered_by_role_then_number():
    team = ["experiment-10", "todo", "experiment-2", "manager"]
    assert sorted(team, key=_team_order(ROLES)) == ["experiment-2", "experiment-10", "manager", "todo"]
