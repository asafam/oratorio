"""Turning a name the user typed into an agent -- no database or tmux needed."""
from __future__ import annotations

import pytest
import yaml

from orchestration.session.up import _team_order, model_for, parse_agents, iterm_script, resolve_agent, role_files, tile_order

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
    team = ["experiment-10", "reviewer", "experiment-2", "manager"]
    assert sorted(team, key=_team_order(ROLES)) == ["experiment-2", "experiment-10", "manager", "reviewer"]


def test_model_comes_from_the_role_file_when_nothing_else_is_set():
    assert model_for("manager", "opus", {}) == "opus"
    assert model_for("manager", None, {"model": None, "models": {}}) == "sonnet"


def test_a_model_the_user_set_wins_over_the_role_file():
    assert model_for("manager", "opus", {"model": "haiku"}) == "haiku"


def test_an_agents_own_model_wins_over_the_teams():
    state = {"model": "haiku", "models": {"experiment-2": "opus"}}
    assert model_for("experiment-2", "sonnet", state) == "opus"
    assert model_for("experiment-1", "sonnet", state) == "haiku"


def test_agents_can_be_a_plain_list_of_names():
    assert parse_agents(["manager", "experiment-1"]) == [("manager", {}), ("experiment-1", {})]


def test_agents_can_carry_their_own_settings():
    text = "manager:\n  model: opus\nexperiment-1:\nexperiment-2: {model: haiku}\n"
    assert parse_agents(yaml.safe_load(text)) == [
        ("manager", {"model": "opus"}), ("experiment-1", {}), ("experiment-2", {"model": "haiku"})]
    assert parse_agents(["manager", {"writer": {"model": "opus"}}]) == [
        ("manager", {}), ("writer", {"model": "opus"})]


def test_a_mistyped_agent_setting_is_refused():
    with pytest.raises(ValueError, match="model"):
        parse_agents({"manager": {"modle": "opus"}})
    with pytest.raises(ValueError):
        parse_agents({"manager": "opus"})


def test_numbered_agents_are_tiled_side_by_side():
    # 4 agents + the board = 5 tiles, which tmux lays out 2 across.
    team = ["manager", "experiment-1", "experiment-2", "reviewer"]
    assert tile_order(team, ROLES, 5) == ["manager", "reviewer", "experiment-1", "experiment-2"]


def test_numbered_agents_go_first_when_the_others_do_not_fill_a_row():
    team = ["manager", "experiment-1", "experiment-2", "reviewer", "writer"]
    assert tile_order(team, ROLES, 6) == ["experiment-1", "experiment-2", "manager", "reviewer", "writer"]


def test_a_chosen_tile_order_is_kept_and_the_rest_follow():
    team = ["manager", "experiment-1", "experiment-2", "reviewer"]
    assert tile_order(team, ROLES, 5, ["reviewer", "experiment-2"]) == [
        "reviewer", "experiment-2", "experiment-1", "manager"]
    # An agent that has since been removed is skipped.
    assert tile_order(team, ROLES, 5, ["writer", "manager"])[0] == "manager"


def test_iterm_window_is_split_into_the_same_grid_row_by_row():
    script = iterm_script(["a", "b", "c", "d", "e"])  # 5 tiles: 2 across, 3 down
    assert "tell s0 to set s2 to (split horizontally" in script   # row 2 under row 1
    assert "tell s2 to set s4 to (split horizontally" in script   # row 3 under row 2
    assert "tell s0 to set s1 to (split vertically" in script     # a | b
    assert "tell s2 to set s3 to (split vertically" in script     # c | d
    assert "set s5" not in script and "tell s4 to set" not in script  # e has its row to itself
    assert 'tell s4 to write text (ASCII character 21) & "e"' in script


def test_iterm_commands_are_quoted_for_applescript():
    assert '& "say \\"hi\\""' in iterm_script(['say "hi"'])
