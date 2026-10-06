#!/usr/bin/env python3
"""Run a team of agents in one tiled tmux window: one pane per agent, plus
the board view. Each agent is an ordinary interactive Claude Code session
you can watch and type in; a listener per agent (second tmux window) types
board messages into its pane whenever it is idle.

    oratorio up [-w NAME] [--only a,b] [--model M]    start a workspace
    oratorio add <agent> [-w NAME]                     add one while running
    oratorio remove <agent> [-w NAME]                  close one
    oratorio restart <agent>|--all [-w NAME]           start it afresh, where it is
    oratorio tile [agent ...] [-w NAME]                arrange the tiles
    oratorio save [-w NAME]                            write its yaml file
    oratorio attach [-w NAME]                          open its tiles
    oratorio open <agent|board> [-w NAME]              show one agent alone in this terminal
    oratorio open --all [--tab|--here] [-w NAME]       iTerm2: one split each, in a new window,
                                                       a new tab, or under this terminal
    oratorio status                                    every running workspace
    oratorio down [-w NAME | --all]

(`oratorio` is bin/oratorio; or run this file with the venv's python.)

A workspace is a name plus a team. Several can run at once -- on the
board they are separate: `manager` in one never sees messages from
another (schema/004_workspaces.sql). Each gets its own tmux session,
`oratorio-<name>`.

A workspace can be described in a yaml file, in any folder:

    name: thesis
    workdir: .                   # where its agents work; default: the file's folder
    agents:
      manager:
        model: opus              # optional: a model for this agent
      experiment-1:
      experiment-2:
      reviewer:
        runner: codex            # optional: run it in Codex instead of Claude Code
    model: sonnet                # optional: one model for every agent
    permission_mode: auto        # optional; this is the default

(`agents: [manager, experiment-1]` also works when no agent needs a
setting of its own.)

Which model an agent gets -- the most specific thing you said wins: the
`model` under that agent, then the team's `model:`, then the role file's
own `model`. `up --model M` puts every agent on M for that run;
`add <agent> --model M` sets it for that one agent.

Which program an agent runs in (`runner`: claude or codex) is chosen the
same way: under the agent, then the team's `runner:`, then the role
file's, else claude. A model setting only counts for agents on the same
runner it was given for: a team `model: sonnet` leaves the Codex agents
on Codex's own default model.

Name the file `oratorio.yaml`, or `<anything>.oratorio.yaml` to keep
several in one folder. `-w NAME` always says which workspace you mean.
Leaving it out is a shortcut: `up` uses the folder's only workspace file
(or, with none, names the workspace after the folder); the other commands
use the folder's running workspace, or the only one running.

Agents come from the role files in orchestration/roles/*.md. A role
marked `multiple: true` can run as several numbered agents: `add
experiment` starts `experiment-1`, again gives `experiment-2`, and so on.

Each agent gets a fresh token when it starts (its previous one stops
working), so nothing secret is kept between runs; per-run files live in
.oratorio/<workspace>/ (gitignored). Nothing here spends tokens on its
own: sessions start idle and only work when a message, or you, gives
them something.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from orchestration.board_core import auth, db, registry  # noqa: E402
from orchestration.roles.sync_roles import ROLES_DIR, git_blob_sha, parse_role_file  # noqa: E402

RUN_ROOT = REPO / ".oratorio"
WORKSPACE_FILE = "oratorio.yaml"
WORKSPACE_SUFFIX = ".oratorio.yaml"
NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,40}")

# The workspace this invocation is acting on -- set by use_workspace().
WORKSPACE = ""
SESSION = ""
RUN_DIR = RUN_ROOT
STATE_FILE = RUN_ROOT / "state.json"


def use_workspace(name: str) -> None:
    global WORKSPACE, SESSION, RUN_DIR, STATE_FILE
    if not NAME_RE.fullmatch(name):
        sys.exit(f"'{name}' is not a usable workspace name: lowercase letters, digits, - and _ only.")
    WORKSPACE = name
    SESSION = f"oratorio-{name}"
    RUN_DIR = RUN_ROOT / name
    STATE_FILE = RUN_DIR / "state.json"


def slug(text: str) -> str:
    """A folder or file name turned into a workspace name."""
    return re.sub(r"[^a-z0-9_-]+", "-", text.lower()).strip("-_")[:41] or "workspace"


def workspace_files(folder: Path) -> dict[str, Path]:
    """Workspace name -> yaml file, for the workspace files in `folder`."""
    found = {}
    for path in sorted(folder.glob("*" + WORKSPACE_SUFFIX)) + [folder / WORKSPACE_FILE]:
        if not path.is_file() or path in found.values():
            continue
        data = yaml.safe_load(path.read_text()) or {}
        fallback = folder.resolve().name if path.name == WORKSPACE_FILE else path.name[: -len(WORKSPACE_SUFFIX)]
        found[data.get("name") or slug(fallback)] = path
    return found


def running_workspaces() -> list[str]:
    if not RUN_ROOT.exists():
        return []
    sessions = subprocess.run(["tmux", "list-sessions", "-F", "#{session_name}"],
                              capture_output=True, text=True).stdout.split()
    return sorted(d.name for d in RUN_ROOT.iterdir()
                  if d.is_dir() and (d / "state.json").exists() and f"oratorio-{d.name}" in sessions)


def pick_running_workspace(args) -> None:
    """For every command except `up`: which running workspace is meant?"""
    if args.workspace:
        return use_workspace(args.workspace)
    running = running_workspaces()
    if not running:
        sys.exit("Nothing is running.")
    here = [name for name in workspace_files(Path.cwd()) if name in running]
    if not here:  # no workspace file here: the workspace whose agents work in this folder
        cwd = str(Path.cwd().resolve())
        here = [name for name in running
                if json.loads((RUN_ROOT / name / "state.json").read_text()).get("workdir") == cwd]
    if len(here) == 1:
        return use_workspace(here[0])
    if len(running) == 1:
        return use_workspace(running[0])
    sys.exit(f"Several workspaces are running ({', '.join(running)}). Say which one with -w NAME.")
# Claude Code's auto mode: the agent goes ahead with what it judges safe and
# stops to ask only for the rest, so a team is not left waiting on approvals.
DEFAULT_PERMISSION_MODE = "auto"
MCP_SERVER_NAME = "orchestration-board"

PROTOCOL = """\
# You are `{agent_id}`, one agent in the `{workspace}` team, which works through a shared message board

You never talk to the other agents directly -- only through the board
tools (`{mcp}`), and they answer the same way. The team changes while you
work: call `list_agents` to see who is on it right now.

## How messages reach you

- A line starting with `[board message <id> from <sender> ...]` was sent
  to you by another agent and typed into this session for you.
- A line starting with `[board note: ...]` is information from the board
  itself (a missed reply deadline, a new thread).
- Anything else is the human operator typing to you directly.

## What to do with a board message

1. Do what it asks.
2. To answer, call `post_message` with `recipient=<sender>`,
   `msg_type="REPLY"` and `in_reply_to=<id>`.
3. When you have finished with it, call `ack_message(<id>)`. Until you do,
   it counts as still open and will be handed to you again if your
   session restarts. If you are handed an id you already finished, just
   ack it.

## Sending messages

- `post_message` to one agent (`recipient=`) or to a topic (`topic=`).
- The other agent may be busy or offline for minutes or hours. By default
  your message waits for it. If it only matters right now, set
  `expires_in_seconds`. If you need an answer by some time, set
  `reply_within_seconds` -- you will be told if none arrived.
- Give related messages the same `thread_id`; replies inherit it.
- After posting, finish your turn. Do NOT wait, sleep or poll for the
  answer -- it is typed in here when it arrives.
- Every message wakes the agent it goes to, and that costs tokens. Send
  one only when the other agent has to know or do something. Never send
  a message just to say thanks, ok or "received" -- `ack_message` already
  says that. Post to a topic only what every subscriber needs.
- When you have nothing to do, do nothing: finish your turn and stay
  idle. Do not check on others or look for work on your own.
- `read_messages` shows what is still open for you -- check it if your
  context was cleared or compacted.

## Keep your context small

For heavy work (long runs, reading many files, large outputs) use a
subagent and keep only its summary.

---

"""


def load_dotenv() -> None:
    """Pick up ORCH_BOARD_DSN etc. from the repo's .env if not already set."""
    env_file = REPO / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def tmux(*args: str) -> str:
    return subprocess.run(["tmux", *args], check=True, capture_output=True, text=True).stdout.strip()


def session_exists() -> bool:
    return subprocess.run(["tmux", "has-session", "-t", SESSION], capture_output=True).returncode == 0


def role_files() -> dict[str, Path]:
    """role name -> its role file, in file-name order."""
    return {parse_role_file(p)[0]["agent_id"]: p for p in sorted(ROLES_DIR.glob("*.md"))}


def is_multiple(path: Path) -> bool:
    return bool(parse_role_file(path)[0].get("multiple", False))


def resolve_agent(name: str, roles: dict[str, Path], taken: set[str]) -> tuple[str, str]:
    """Turn what the user typed into (agent_id, role).

    A normal role is its own single agent (`manager`). A role marked
    `multiple: true` runs as numbered agents: naming the role
    (`experiment`) picks the lowest number not in `taken`
    (`experiment-1`, then `experiment-2`, ...); naming a numbered agent
    (`experiment-3`) means exactly that one. Raises ValueError otherwise.
    """
    if name in roles:
        if not is_multiple(roles[name]):
            return name, name
        n = 1
        while f"{name}-{n}" in taken:
            n += 1
        return f"{name}-{n}", name
    numbered = re.fullmatch(r"(.+)-(\d+)", name)
    if numbered and numbered.group(1) in roles and is_multiple(roles[numbered.group(1)]):
        return name, numbered.group(1)
    raise ValueError(f"No role for '{name}'. Roles: {', '.join(roles)}")


def sync_agent(conn, agent_id: str, path: Path) -> tuple[dict, str, str]:
    """Load one role file into the board as the agent called `agent_id`
    in the current workspace, and give it a fresh token. Returns
    (frontmatter, brief, token)."""
    meta, brief = parse_role_file(path)
    brief = brief.replace("{agent_id}", agent_id)
    topics = meta.get("topics", [])
    for topic in topics:  # a role may name a topic nobody has seeded yet
        conn.execute(
            "INSERT INTO board.topic (topic) VALUES (%s) ON CONFLICT DO NOTHING", (topic,)
        )
    token = auth.generate_token()
    key = registry.upsert_agent(
        conn,
        name=agent_id,
        workspace=WORKSPACE,
        role_doc_path=str(path.relative_to(REPO)),
        role_version=git_blob_sha(path),
        brief=brief,
        peers=[],
        topics=topics,
        auth_token_hash=auth.hash_token(token),
        is_auditor=bool(meta.get("is_auditor", False)),
    )
    registry.set_auth_token_hash(conn, key, auth.hash_token(token))
    return meta, brief, token


AGENT_SETTINGS = {"model", "runner"}
RUNNERS = ("claude", "codex")


def parse_agents(entries) -> list[tuple[str, dict]]:
    """The `agents:` part of a workspace file as (name, settings) pairs.

    Either a plain list of names, or names with their own settings under
    them (`manager: {model: opus}`; nothing under a name means no
    settings). Raises ValueError for anything else."""
    if isinstance(entries, dict):
        pairs = list(entries.items())
    elif isinstance(entries, list):
        pairs = []
        for entry in entries:
            if isinstance(entry, dict) and len(entry) == 1:
                pairs.extend(entry.items())
            else:
                pairs.append((entry, None))
    else:
        raise ValueError("`agents:` must be a list of agent names, or names with settings under them.")
    agents = []
    for name, settings in pairs:
        if not isinstance(name, str):
            raise ValueError(f"`agents:` has an entry that is not an agent name: {name!r}")
        settings = {} if settings is None else settings
        if not isinstance(settings, dict) or set(settings) - AGENT_SETTINGS:
            raise ValueError(f"Under agent '{name}' only these can be set: {', '.join(sorted(AGENT_SETTINGS))}")
        agents.append((name, settings))
    return agents


def runner_for(agent_id: str, role_runner: str | None, state: dict) -> str:
    """The program one agent runs in, claude or codex. Chosen like its
    model: this agent, then the whole team, then the role file."""
    return (state.get("runners") or {}).get(agent_id) or state.get("runner") or role_runner or "claude"


def model_for(agent_id: str, role_model: str | None, state: dict,
              runner: str = "claude", role_runner: str | None = None) -> str | None:
    """The model one agent runs on. What the user set wins over the role
    file, and the more specific setting wins: this agent, then the whole
    team. The team's and the role's model only count if they were given
    for the runner this agent is on (a Claude model means nothing to
    Codex). None: Codex picks its own default."""
    own = (state.get("models") or {}).get(agent_id)
    team = state.get("model") if (state.get("runner") or "claude") == runner else None
    role = role_model if (role_runner or "claude") == runner else None
    return own or team or role or ("sonnet" if runner == "claude" else None)


def setup_for(agent_id: str, meta: dict, state: dict) -> tuple[str, str | None]:
    """(runner, model) for one agent, from its role file's frontmatter and the state."""
    runner = runner_for(agent_id, meta.get("runner"), state)
    return runner, model_for(agent_id, meta.get("model"), state, runner, meta.get("runner"))


def model_label(runner: str, model: str | None) -> str:
    """What is shown after an agent's name: `fable`, `codex: gpt-5.5`, `codex`."""
    if runner == "claude":
        return model or "sonnet"
    return f"{runner}: {model}" if model else runner


# Claude Code's permission modes as Codex options. `auto` hands approval
# prompts to Codex's own reviewer, the nearest thing to Claude's auto mode.
CODEX_PERMISSIONS = {
    "auto": ["--approve-for-me"],
    "acceptEdits": ["--sandbox", "workspace-write", "--ask-for-approval", "on-request"],
    "default": ["--sandbox", "workspace-write", "--ask-for-approval", "on-request"],
    "plan": ["--sandbox", "read-only", "--ask-for-approval", "on-request"],
}


def check_runner(runner: str, permission_mode: str) -> None:
    """Exit with a clear message if this runner can't be started as asked."""
    if runner not in RUNNERS:
        sys.exit(f"Unknown runner '{runner}'. Use one of: {', '.join(RUNNERS)}")
    if runner == "codex":
        if permission_mode not in CODEX_PERMISSIONS:
            sys.exit(f"permission_mode '{permission_mode}' has no Codex equivalent here. "
                     f"For Codex agents use one of: {', '.join(CODEX_PERMISSIONS)}")
        if not shutil.which("codex"):
            sys.exit("A Codex agent needs the `codex` command, and it is not on your PATH.")


def write_private(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o600)


# The user's own Claude Code settings an agent takes over. Agents skip the
# rest of the user's setup (plugins, hooks, MCP servers -- see the launch
# command below), but these only change what the user sees and is asked:
# how auto mode behaves for them and, for a role marked `status_line:
# true`, their status line. (A status line takes rows from a small tile,
# and its account-wide parts read the same on every agent, so it is not
# on everywhere.)
CARRIED_SETTINGS = ("skipAutoPermissionPrompt", "autoMode")


def carried_settings(status_line: bool = False, settings_file: Path | None = None) -> dict:
    """The CARRIED_SETTINGS found in the user's Claude Code settings file,
    with their status line too if `status_line`."""
    if settings_file is None:
        home = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
        settings_file = home / "settings.json"
    try:
        mine = json.loads(settings_file.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(mine, dict):
        return {}
    wanted = CARRIED_SETTINGS + (("statusLine",) if status_line else ())
    return {key: mine[key] for key in wanted if key in mine}


def agent_command(agent_id: str, meta: dict, brief: str, token: str, state: dict) -> tuple[str, Path]:
    """Write this agent's per-run files; return (shell command for its
    pane, its idle-flag path)."""
    idle_flag = RUN_DIR / f"{agent_id}.idle"
    idle_flag.unlink(missing_ok=True)
    Path(str(idle_flag) + ".cleared").unlink(missing_ok=True)
    instructions = PROTOCOL.format(agent_id=agent_id, workspace=WORKSPACE, mcp=MCP_SERVER_NAME) + brief + "\n"
    runner, model = setup_for(agent_id, meta, state)
    if runner == "codex":
        return codex_command(agent_id, model, instructions, token, state, idle_flag), idle_flag

    mcp_config = RUN_DIR / f"{agent_id}.mcp.json"
    write_private(mcp_config, json.dumps({"mcpServers": {MCP_SERVER_NAME: {
        "command": sys.executable,
        "args": [str(REPO / "orchestration" / "mcp" / "server.py")],
        "env": {"ORCH_BOARD_DSN": os.environ["ORCH_BOARD_DSN"], "ORCH_AGENT_TOKEN": token},
    }}}, indent=2))

    # The agent's own hooks keep the idle flag: present while it waits for
    # input, gone while it works. This is what tells the listener when it
    # is safe to type (see session/tmux.py).
    flag = shlex.quote(str(idle_flag))
    hook = lambda command: {"hooks": [{"type": "command", "command": command}]}  # noqa: E731
    settings = RUN_DIR / f"{agent_id}.settings.json"
    write_private(settings, json.dumps({**carried_settings(bool(meta.get("status_line"))), "hooks": {
        "SessionStart": [hook(f"touch {flag}"),
                         {"matcher": "clear", **hook(f"touch {flag}.cleared")}],
        "UserPromptSubmit": [hook(f"rm -f {flag}")],
        "Stop": [hook(f"touch {flag}")],
    }}, indent=2))

    prompt = RUN_DIR / f"{agent_id}.prompt.md"
    prompt.write_text(instructions)

    command = [
        "claude", "-n", agent_id,
        "--model", model,
        # Skip user-level plugins/hooks/MCP servers: an agent needs the
        # board and its role, not everything installed on this machine.
        "--setting-sources", "project",
        "--strict-mcp-config", "--mcp-config", str(mcp_config),
        "--settings", str(settings),
        "--append-system-prompt-file", str(prompt),
        "--permission-mode", state["permission_mode"],
        "--allowedTools", f"mcp__{MCP_SERVER_NAME}",
    ]
    return shlex.join(command), idle_flag


def codex_command(agent_id: str, model: str | None, instructions: str, token: str, state: dict,
                  idle_flag: Path) -> str:
    """The same agent, in Codex. Everything is given as `-c` settings on
    top of the user's own ~/.codex/config.toml, which Codex always loads.

    No hooks: Codex only runs hooks you have trusted by hand. So the idle
    flag is put there before Codex starts and again by Codex's `notify`
    at the end of every turn; the listener checks the screen as well
    (see session/tmux.py). The token is not on the command line, where
    `ps` would show it: it sits in a private start-up script, and Codex
    passes it on to the board's MCP server (`env_vars`)."""
    toml = lambda value: json.dumps(value, ensure_ascii=False)  # noqa: E731 -- JSON strings and lists are TOML
    server = f"mcp_servers.{MCP_SERVER_NAME}"
    settings = {
        f"{server}.command": toml(sys.executable),
        f"{server}.args": toml([str(REPO / "orchestration" / "mcp" / "server.py")]),
        f"{server}.env_vars": toml(["ORCH_BOARD_DSN", "ORCH_AGENT_TOKEN"]),
        f"{server}.default_tools_approval_mode": toml("approve"),  # the board's tools never ask
        # Codex adds a JSON argument to this command; with `sh -c` it lands in $0, unused.
        "notify": toml(["sh", "-c", f"touch {shlex.quote(str(idle_flag))}"]),
        "developer_instructions": toml(instructions),
        "check_for_update_on_startup": "false",
        # Draw in the pane's normal screen, as Claude Code does: in its own
        # full screen Codex takes the mouse, and the wheel no longer scrolls back.
        "tui.alternate_screen": toml("never"),
    }
    command = ["codex", *CODEX_PERMISSIONS[state["permission_mode"]]]
    if model:
        command += ["--model", model]
    for key, value in settings.items():
        command += ["-c", f"{key}={value}"]
    script = RUN_DIR / f"{agent_id}.codex.sh"
    write_private(script, "".join([
        f"export ORCH_BOARD_DSN={shlex.quote(os.environ['ORCH_BOARD_DSN'])}\n",
        f"export ORCH_AGENT_TOKEN={shlex.quote(token)}\n",
        f"touch {shlex.quote(str(idle_flag))}\n",
        f"exec {shlex.join(command)}\n",
    ]))
    return f"sh {shlex.quote(str(script))}"


# ---------------------------------------------------------------------
# What is running. The per-agent .env file is the record (pane titles
# can't be: Claude Code rewrites them) -- an agent counts as running
# while the pane named there still exists.
# ---------------------------------------------------------------------
def _read_env(path: Path) -> dict[str, str]:
    return dict(
        (k, shlex.split(v)[0] if v else "")
        for k, v in (line.split("=", 1) for line in path.read_text().splitlines() if "=" in line)
    )


def running_agents() -> dict[str, dict[str, str]]:
    """agent_id -> its env record, for agents whose pane is still alive."""
    if not RUN_DIR.exists() or not session_exists():
        return {}
    live = set(tmux("list-panes", "-s", "-t", SESSION, "-F", "#{pane_id}").split())
    found = {}
    for env_file in sorted(RUN_DIR.glob("*.env")):
        env = _read_env(env_file)
        if env.get("ORCH_TMUX_PANE") in live:
            found[env_file.stem] = env
    return found


def load_state() -> dict:
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}


def forget_agent(agent_id: str) -> None:
    for f in RUN_DIR.glob(f"{agent_id}.*"):
        f.unlink()


def window_exists(name: str) -> bool:
    return name in tmux("list-windows", "-t", SESSION, "-F", "#{window_name}").split()


def tile_order(agents: list[str], roles: dict[str, Path], tiles: int,
               chosen: list[str] | None = None) -> list[str]:
    """The order to lay agent tiles out in. tmux's `tiled` layout fills a
    grid row by row, in this order.

    With `chosen` (what the user asked for with `oratorio tile`): those
    agents in that order, then any others. Without: numbered agents of one
    role (`experiment-1`, `experiment-2`) are put side by side. A group is
    only side by side if it starts at the beginning of a row, so the
    one-of-a-kind agents go first when they fill whole rows; otherwise the
    numbered ones do. `tiles` is how many tiles the window has in all."""
    if chosen:
        first = [a for a in chosen if a in agents]
        return first + sorted((a for a in agents if a not in first), key=_team_order(roles))
    _, columns = grid(tiles)
    ordered = sorted(agents, key=_team_order(roles))
    numbered = [a for a in ordered if is_multiple(roles[resolve_agent(a, roles, set())[1]])]
    single = [a for a in ordered if a not in numbered]
    return single + numbered if len(single) % columns == 0 else numbered + single


def view_session(agent_id: str) -> str:
    """The tmux session behind `oratorio open <agent>`: one terminal's own
    view of the workspace, so it can show a different window from the rest."""
    return f"view-{SESSION}--{agent_id}"


def close_views(agent_id: str | None = None) -> None:
    """End the `open` views of one agent, or of the whole workspace."""
    prefix = view_session(agent_id or "")
    for name in tmux("list-sessions", "-F", "#{session_name}").split():
        if name == prefix or (agent_id is None and name.startswith(prefix)):
            subprocess.run(["tmux", "kill-session", "-t", f"={name}"], capture_output=True)


# What a tile shows along its top edge: its label if it has one, else its title.
BORDER_FORMAT = " #{?#{@label},#[bold]#{@label}#[default],#{pane_title}} "


def agent_label(agent_id: str) -> str:
    """How an agent is named on screen: its name and the model it runs on,
    `manager (fable)`."""
    record = running_agents().get(agent_id, {})
    if "ORCH_MODEL" in record:
        runner, model = record.get("ORCH_RUNNER", "claude"), record["ORCH_MODEL"] or None
    else:  # started before the model was written down: what it would get now
        try:
            path = role_files()[resolve_agent(agent_id, role_files(), set())[1]]
            runner, model = setup_for(agent_id, parse_role_file(path)[0], load_state())
        except ValueError:
            return agent_id
    return f"{agent_id} ({model_label(runner, model)})"


def label_pane(pane: str, label: str) -> None:
    tmux("set-option", "-p", "-t", pane, "@label", label)


def views_open() -> bool:
    """Is anything of this workspace being shown with `oratorio open`?"""
    return any(name.startswith(view_session(""))
               for name in tmux("list-sessions", "-F", "#{session_name}").split())


def retile() -> None:
    if window_exists("agents"):
        panes = tmux("list-panes", "-t", f"{SESSION}:agents", "-F", "#{pane_id}").split()
        agents = {a: env["ORCH_TMUX_PANE"] for a, env in running_agents().items()
                  if env["ORCH_TMUX_PANE"] in panes}
        # Agents in order, then whatever else is there (the board view) as it was.
        order = tile_order(list(agents), role_files(), len(panes), load_state().get("tiles"))
        wanted = [agents[a] for a in order]
        wanted += [pane for pane in panes if pane not in wanted]
        for i, pane in enumerate(wanted):
            if panes[i] != pane:
                tmux("swap-pane", "-d", "-s", pane, "-t", panes[i])
                j = panes.index(pane)
                panes[i], panes[j] = panes[j], panes[i]
    for window in ("agents", "listeners"):
        if window_exists(window):
            tmux("select-layout", "-t", f"{SESSION}:{window}", "tiled")


def start_agent(conn, agent_id: str, path: Path, state: dict, replace: dict | None = None) -> None:
    """Everything for one agent: board row + fresh token, its session
    pane, and its listener pane. Creates the tmux session/windows if this
    is the first one.

    With `replace` (the running agent's own record, for a restart): the
    new session and listener take the place of the old ones in the same
    panes, so the agent stays where it is on screen."""
    # Before sync_agent, which replaces the running agent's token.
    check_runner(setup_for(agent_id, parse_role_file(path)[0], state)[0], state["permission_mode"])
    meta, brief, token = sync_agent(conn, agent_id, path)
    conn.commit()
    runner, model = setup_for(agent_id, meta, state)
    command, idle_flag = agent_command(agent_id, meta, brief, token, state)
    workdir = state["workdir"]

    if replace:
        pane = replace["ORCH_TMUX_PANE"]
        tmux("respawn-pane", "-k", "-t", pane, "-c", workdir, command)
    elif not session_exists():
        pane = tmux("new-session", "-d", "-s", SESSION, "-n", "agents", "-x", "250", "-y", "60",
                    "-c", workdir, "-P", "-F", "#{pane_id}", command)
        tmux("set-option", "-t", SESSION, "pane-border-status", "top")
        tmux("set-option", "-t", SESSION, "pane-border-format", BORDER_FORMAT)
        tmux("set-option", "-t", SESSION, "mouse", "on")  # click a tile to type in it
        pass_modified_keys()
    elif views_open():  # agents are shown in terminals of their own: no tile, a window to `open`
        pane = tmux("new-window", "-d", "-t", f"{SESSION}:", "-n", agent_id, "-c", workdir,
                    "-P", "-F", "#{pane_id}", command)
        own_window(agent_id, pane)
    elif not window_exists("agents"):
        pane = tmux("new-window", "-d", "-t", f"{SESSION}:", "-n", "agents", "-c", workdir,
                    "-P", "-F", "#{pane_id}", command)
    else:
        pane = tmux("split-window", "-d", "-t", f"{SESSION}:agents", "-c", workdir,
                    "-P", "-F", "#{pane_id}", command)
    tmux("select-pane", "-t", pane, "-T", agent_id)
    label_pane(pane, f"{agent_id} ({model_label(runner, model)})")
    retile()

    env = {
        "ORCH_MODEL": model or "",
        "ORCH_BOARD_DSN": os.environ["ORCH_BOARD_DSN"],
        "ORCH_AGENT_TOKEN": token,
        "ORCH_RUNNER": runner,
        "ORCH_TMUX_PANE": pane,
        "ORCH_IDLE_FLAG": str(idle_flag),
    }
    env_file = RUN_DIR / f"{agent_id}.env"
    write_private(env_file, "".join(f"{k}={shlex.quote(v)}\n" for k, v in env.items()))
    listener = (f"set -a; . {shlex.quote(str(env_file))}; set +a; "
                f"exec {shlex.quote(sys.executable)} -m orchestration.listener.listen")
    live = tmux("list-panes", "-s", "-t", SESSION, "-F", "#{pane_id}").split()
    # The listeners live in a second window (plain code; they only show log lines).
    if replace and replace.get("ORCH_LISTENER_PANE") in live:
        listener_pane = replace["ORCH_LISTENER_PANE"]  # the old one's token no longer works
        tmux("respawn-pane", "-k", "-t", listener_pane, "-c", str(REPO), listener)
    elif not window_exists("listeners"):
        listener_pane = tmux("new-window", "-d", "-t", SESSION, "-n", "listeners", "-c", str(REPO),
                             "-P", "-F", "#{pane_id}", listener)
    else:
        listener_pane = tmux("split-window", "-d", "-t", f"{SESSION}:listeners", "-c", str(REPO),
                             "-P", "-F", "#{pane_id}", listener)
    tmux("select-pane", "-t", listener_pane, "-T", f"listener: {agent_id}")
    with env_file.open("a") as f:
        f.write(f"ORCH_LISTENER_PANE={shlex.quote(listener_pane)}\n")
    retile()


def pass_modified_keys() -> None:
    """Let Shift+Return and the like reach the agents as themselves. Left
    alone, tmux hands Shift+Return on as a plain Return, which sends the
    message instead of starting a new line."""
    tmux("set-option", "-s", "extended-keys", "on")
    if "extkeys" not in tmux("show-options", "-s", "terminal-features"):
        # Ask the terminal outside tmux (iTerm2, ...) to report those keys too.
        tmux("set-option", "-as", "terminal-features", "xterm*:extkeys")


def keep_tunnel() -> None:
    """With the board on a remote server (ORATORIO_BOARD_HOST is set):
    keep the SSH tunnel to it open for as long as the workspace runs, in a
    pane beside the listeners. Without the tunnel no agent can post or be
    reached, and on its own it does not come back after a network change."""
    if not os.environ.get("ORATORIO_BOARD_HOST") or not window_exists("listeners"):
        return
    titles = tmux("list-panes", "-t", f"{SESSION}:listeners", "-F", "#{pane_title}").splitlines()
    if "tunnel" in titles:
        return
    script = shlex.quote(str(REPO / "orchestration" / "ops" / "scripts" / "board_tunnel.sh"))
    host = shlex.quote(os.environ["ORATORIO_BOARD_HOST"])
    pane = tmux("split-window", "-d", "-t", f"{SESSION}:listeners", "-c", str(REPO), "-P", "-F",
                "#{pane_id}", f"ORATORIO_BOARD_HOST={host} {script} keep")
    tmux("select-pane", "-t", pane, "-T", "tunnel")
    retile()


def start_board() -> None:
    """Start the board view as a tile (or, with no tiles left, in a window
    of its own). If it is already there, start it afresh where it is."""
    command = f"{shlex.quote(sys.executable)} -m orchestration.watch.board -w {shlex.quote(WORKSPACE)}"
    if "board" in openable():
        tmux("respawn-pane", "-k", "-t", openable()["board"], "-c", str(REPO), command)
        return
    where = (["split-window", "-d", "-t", f"{SESSION}:agents"] if window_exists("agents")
             else ["new-window", "-d", "-t", f"{SESSION}:", "-n", "agents"])
    board = tmux(*where, "-c", str(REPO), "-P", "-F", "#{pane_id}", command)
    tmux("select-pane", "-t", board, "-T", f"BOARD: {WORKSPACE}")
    label_pane(board, "board")
    retile()


def connect():
    if not os.environ.get("ORCH_BOARD_DSN"):
        sys.exit("ORCH_BOARD_DSN is not set (put it in .env, or export it).")
    try:
        pool = db.get_pool()
        pool.wait(timeout=10)
        return pool
    except Exception as e:
        sys.exit(f"Cannot reach the board database ({e}). Is the tunnel up? "
                 "See orchestration/ops/scripts/board_tunnel.sh")


def cmd_up(args) -> None:
    folder = Path(args.workdir or ".").resolve()
    files = workspace_files(folder)
    if args.workspace:
        name = args.workspace
    elif len(files) == 1:
        name = next(iter(files))
    elif not files:
        name = slug(folder.name)
    else:
        sys.exit(f"This folder has several workspaces ({', '.join(files)}). Say which one with -w NAME.")
    use_workspace(name)
    if session_exists():
        sys.exit(f"Workspace '{name}' is already running. Open it with `oratorio attach -w {name}`, "
                 "add agents with `add`, or stop it with `down`.")

    file = files.get(name)
    workspace = (yaml.safe_load(file.read_text()) or {}) if file else {}
    workdir = (file.parent / workspace["workdir"]).resolve() if workspace.get("workdir") else folder
    if not workdir.is_dir():
        sys.exit(f"Working folder does not exist: {workdir}")

    roles = role_files()
    def resolve_all(names) -> dict[str, str]:  # agent name -> role
        team: dict[str, str] = {}
        for entry in names:
            agent_id, role = resolve_agent(entry, roles, set(team))
            team[agent_id] = role
        return team

    # Command line wins over the workspace file, which wins over "one per role".
    try:
        listed = parse_agents(workspace["agents"]) if workspace.get("agents") else [(r, {}) for r in roles]
        in_file = resolve_all(name for name, _ in listed)
        wanted = resolve_all(args.only.split(",")) if args.only else in_file
    except ValueError as e:
        sys.exit(str(e))
    # `--model` is for every agent, so it also replaces the file's per-agent models
    # (and `--runner` the per-agent runners).
    def per_agent(key: str) -> dict[str, str]:
        return {} if getattr(args, key) else {
            agent_id: settings[key]
            for agent_id, (_, settings) in zip(in_file, listed) if settings.get(key)
        }

    state = {
        "name": name,
        "workdir": str(workdir),
        "file": str(file) if file else None,
        "model": args.model or workspace.get("model"),
        "models": per_agent("model"),
        "runner": args.runner or workspace.get("runner"),
        "runners": per_agent("runner"),
        "tiles": workspace.get("tiles"),
        "permission_mode": args.permission_mode or workspace.get("permission_mode")
                           or DEFAULT_PERMISSION_MODE,
    }
    for agent_id, role in wanted.items():  # before anything starts, not halfway through
        check_runner(setup_for(agent_id, parse_role_file(roles[role])[0], state)[0], state["permission_mode"])
    pool = connect()
    RUN_ROOT.mkdir(exist_ok=True)
    RUN_ROOT.chmod(0o700)
    RUN_DIR.mkdir(exist_ok=True)
    for stale in RUN_DIR.iterdir():  # left over from a run that was not shut down cleanly
        stale.unlink()
    STATE_FILE.write_text(json.dumps(state, indent=2))

    with pool.connection() as conn:
        for agent_id, role in wanted.items():
            start_agent(conn, agent_id, roles[role], state)
    db.close_pool()

    start_board()
    keep_tunnel()

    source = f" from {file.name}" if file and not args.only else ""
    print(f"Started workspace '{name}'{source}: {', '.join(wanted)}")
    print(f"Working folder: {workdir}")
    print(f"Open it:  oratorio attach -w {name}")


def cmd_add(args) -> None:
    pick_running_workspace(args)
    if not session_exists():
        sys.exit(f"Workspace '{WORKSPACE}' is not running. Start it with `up` first.")
    roles = role_files()
    running = set(running_agents())
    try:
        agent_id, role = resolve_agent(args.agent, roles, running)
    except ValueError as e:
        sys.exit(str(e))
    if agent_id in running:
        sys.exit(f"'{agent_id}' is already running.")
    state = load_state()
    if args.runner and not args.model:  # a model of the old runner means nothing to the new one
        state = {**state, "models": {a: m for a, m in (state.get("models") or {}).items() if a != agent_id}}
    for key in ("model", "runner"):  # for this agent only; kept, so `save` writes it down
        if getattr(args, key):
            state = {**state, f"{key}s": {**(state.get(f"{key}s") or {}), agent_id: getattr(args, key)}}
    STATE_FILE.write_text(json.dumps(state, indent=2))
    pool = connect()
    with pool.connection() as conn:
        start_agent(conn, agent_id, roles[role], state)
    db.close_pool()
    print(f"Added {agent_id} to '{WORKSPACE}'.")
    if args.here:
        pane = running_agents()[agent_id]["ORCH_TMUX_PANE"]
        was_tiled = int(tmux("display-message", "-p", "-t", pane, "#{window_panes}")) > 1
        own_window(agent_id, pane)
        if was_tiled:
            retile()
        me = shlex.quote(str(REPO / "bin" / "oratorio"))
        session_id = os.environ.get("ITERM_SESSION_ID", "").rpartition(":")[2]
        script = iterm_script([f"{me} open {shlex.quote(agent_id)} -w {shlex.quote(WORKSPACE)}"],
                              "here", session_id=session_id)
        done = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
        if done.returncode != 0:
            sys.exit(f"Could not drive iTerm2 ({done.stderr.strip()}). Make a split yourself and run: "
                     f"oratorio open {agent_id}")
    elif views_open():
        print(f"To see it, make a split or tab and run:  oratorio open {agent_id}")


def cmd_restart(args) -> None:
    pick_running_workspace(args)
    if args.agent == "board" and not args.all:  # the view of the board, not an agent
        if not session_exists():
            sys.exit(f"Workspace '{WORKSPACE}' is not running.")
        start_board()
        print("Restarted the board view. The board itself (the database) was not touched.")
        return
    running = running_agents()
    if bool(args.agent) == args.all:
        sys.exit("Say which agent to restart, or --all (not both).")
    names = list(running) if args.all else [args.agent]
    if not running or names[0] not in running:
        sys.exit(f"'{names[0] if names else args.agent}' is not running in '{WORKSPACE}'. "
                 f"Running: {', '.join(running) or 'nothing'}")
    roles = role_files()
    state = load_state()
    if args.runner and not args.model:  # a model of the old runner means nothing to the new one
        state = {**state, "models": {a: m for a, m in (state.get("models") or {}).items() if a not in names}}
    for key in ("model", "runner"):  # kept, so `save` writes it down
        if getattr(args, key):
            state = {**state, f"{key}s": {**(state.get(f"{key}s") or {}),
                                          **{name: getattr(args, key) for name in names}}}
    STATE_FILE.write_text(json.dumps(state, indent=2))
    pool = connect()
    with pool.connection() as conn:
        for name in names:
            role = resolve_agent(name, roles, set())[1]
            start_agent(conn, name, roles[role], state, replace=running[name])
            print(f"Restarted {agent_label(name)}.")
    db.close_pool()
    print("Each starts with an empty conversation. Messages it had not finished are handed to it again.")


def cmd_remove(args) -> None:
    pick_running_workspace(args)
    record = running_agents().get(args.agent)
    if record is None:
        sys.exit(f"'{args.agent}' is not running in '{WORKSPACE}'.")
    for key in ("ORCH_LISTENER_PANE", "ORCH_TMUX_PANE"):
        if record.get(key):
            subprocess.run(["tmux", "kill-pane", "-t", record[key]], capture_output=True)
    forget_agent(args.agent)
    close_views(args.agent)
    if session_exists():
        retile()
    # Its board row stays: messages sent to it wait in its inbox, and it
    # shows as offline once its heartbeat stops.
    print(f"Removed {args.agent}. Messages sent to it will wait until it is added again.")


def _team_order(roles: dict[str, Path]):
    """Sort key: by role (file order), then by number -- experiment-2 before experiment-10."""
    order = list(roles)

    def key(agent_id: str) -> tuple[int, int]:
        _, role = resolve_agent(agent_id, roles, set())
        number = re.search(r"-(\d+)$", agent_id) if is_multiple(roles[role]) else None
        return order.index(role), int(number.group(1)) if number else 0

    return key


def cmd_save(args) -> None:
    pick_running_workspace(args)
    agents = running_agents()
    if not agents:
        sys.exit("Nothing is running, so there is nothing to save.")
    state = load_state()
    workdir = Path(state["workdir"])
    if state.get("file"):
        target = Path(state["file"])
    else:  # first save: the plain name if it is free, else one named after the workspace
        target = workdir / WORKSPACE_FILE
        if target.exists():
            target = workdir / f"{WORKSPACE}{WORKSPACE_SUFFIX}"

    team = sorted(agents, key=_team_order(role_files()))
    own = {a: {key: state[f"{key}s"][a] for key in ("runner", "model") if a in (state.get(f"{key}s") or {})}
           for a in team}
    workspace = {
        "name": WORKSPACE,
        "workdir": os.path.relpath(workdir, target.parent),
        # Names only, unless some agent has a setting of its own.
        "agents": own if any(own.values()) else team,
    }
    for key in ("runner", "model"):
        if state.get(key):
            workspace[key] = state[key]
    if state.get("tiles"):
        workspace["tiles"] = state["tiles"]
    workspace["permission_mode"] = state["permission_mode"]
    target.write_text(
        f"# Oratorio workspace. Start it from this folder with: oratorio up -w {WORKSPACE}\n"
        + yaml.safe_dump(workspace, sort_keys=False, default_flow_style=None)
    )
    STATE_FILE.write_text(json.dumps({**state, "file": str(target)}, indent=2))
    print(f"Saved '{WORKSPACE}' ({len(agents)} agent(s)) to {target}")


def cmd_tile(args) -> None:
    pick_running_workspace(args)
    running = running_agents()
    if not running:
        sys.exit(f"Workspace '{WORKSPACE}' is not running.")
    unknown = [a for a in args.agents if a not in running]
    if unknown:
        sys.exit(f"Not running in '{WORKSPACE}': {', '.join(unknown)}. Running: {', '.join(running)}")
    STATE_FILE.write_text(json.dumps({**load_state(), "tiles": args.agents or None}, indent=2))
    # Anything shown on its own with `open` comes back into the tiles.
    close_views()
    for name, pane in openable().items():
        if not window_exists("agents"):
            tmux("rename-window", "-t", pane, "agents")
            tmux("set-window-option", "-t", pane, "pane-border-format", BORDER_FORMAT)
        elif tmux("display-message", "-p", "-t", pane, "#{window_name}") != "agents":
            tmux("join-pane", "-d", "-s", pane, "-t", f"{SESSION}:agents")
            tmux("select-layout", "-t", f"{SESSION}:agents", "tiled")  # make room for the next one
    retile()
    order = tile_order(list(running), role_files(), len(running) + 1, args.agents)
    print(f"Tiles, row by row: {', '.join(order)}" + ("" if args.agents else "  (automatic)"))


def cmd_status(args) -> None:
    running = running_workspaces()
    if not running:
        print("Nothing is running.")
        return
    roles = role_files()
    for name in running:
        use_workspace(name)
        agents = running_agents()
        print(f"{name}   ({load_state().get('workdir')})")
        print(f"  running: {', '.join(agents) or '(no agents)'}")
        addable = [r for r, path in roles.items() if is_multiple(path) or r not in agents]
        if addable:
            print(f"  can add: {', '.join(addable)}")


def cmd_attach(args) -> None:
    pick_running_workspace(args)
    if not session_exists():
        sys.exit(f"Workspace '{WORKSPACE}' is not running.")
    # Inside tmux already: switch to it. Otherwise: attach this terminal.
    verb = "switch-client" if os.environ.get("TMUX") else "attach-session"
    os.execvp("tmux", ["tmux", verb, "-t", SESSION])


def openable() -> dict[str, str]:
    """What `oratorio open` can show: each running agent, and `board` -> its pane."""
    panes = {a: env["ORCH_TMUX_PANE"] for a, env in running_agents().items()}
    for line in tmux("list-panes", "-s", "-t", SESSION, "-F", "#{pane_id} #{pane_title}").splitlines():
        pane, _, title = line.partition(" ")
        if title.startswith("BOARD:"):
            panes["board"] = pane
    return panes


def own_window(name: str, pane: str) -> str:
    """Move a pane out of the tiles into a window of its own (unless it
    already has one); return that window's id."""
    if int(tmux("display-message", "-p", "-t", pane, "#{window_panes}")) > 1:
        tmux("break-pane", "-d", "-s", pane, "-n", name)
    window = tmux("display-message", "-p", "-t", pane, "#{window_id}")
    # Size the window to the terminal looking at it. Both are needed: left
    # alone, tmux sizes every window to the terminal last typed in, which
    # leaves dead space in the others.
    tmux("set-window-option", "-t", window, "aggressive-resize", "on")
    tmux("set-window-option", "-t", window, "window-size", "smallest")
    # Its name along the top edge, so it is clear who is in which terminal.
    tmux("set-window-option", "-t", window, "pane-border-status", "top")
    label_pane(pane, name if name == "board" else agent_label(name))
    tmux("set-window-option", "-t", window, "pane-border-format", BORDER_FORMAT)
    return window


def grid(tiles: int) -> tuple[int, int]:
    """(rows, columns) of the grid tmux's `tiled` layout uses for this many tiles."""
    rows = columns = 1
    while rows * columns < tiles:
        rows += 1
        if rows * columns < tiles:
            columns += 1
    return rows, columns


SCREEN_JS = ('ObjC.import("AppKit"); var f=$.NSScreen.mainScreen.visibleFrame, '
             "p=$.NSScreen.screens.objectAtIndex(0).frame; "
             "[f.origin.x, p.size.height-(f.origin.y+f.size.height), f.origin.x+f.size.width, "
             'p.size.height-f.origin.y].map(Math.round).join(", ")')


def screen_bounds() -> str | None:
    """The usable part of the screen in use, as AppleScript window bounds
    ("left, top, right, bottom"); None if macOS will not say."""
    done = subprocess.run(["osascript", "-l", "JavaScript", "-e", SCREEN_JS], capture_output=True, text=True)
    bounds = done.stdout.strip()
    return bounds if done.returncode == 0 and re.fullmatch(r"-?\d+(, -?\d+){3}", bounds) else None


def iterm_script(commands: list[str], where: str = "window", bounds: str | None = None,
                 session_id: str | None = None) -> str:
    """AppleScript that lays the commands out in iTerm2 as a grid of
    splits, one per command, filled row by row. A last row that is not
    full is spread over the whole width.

    `where` is "window" (a new window, filling `bounds` if given), "tab"
    (a new tab in the window in front), or "here": under the split with
    id `session_id` -- the one the command was typed in, which stays on
    top as it is."""
    _, columns = grid(len(commands))
    lines = ['tell application "iTerm2"']
    if where == "here":
        lines += ["set con to missing value",
                  "repeat with w in windows", "repeat with t in tabs of w", "repeat with s in sessions of t",
                  f'if id of s is "{session_id}" then set con to s',
                  "end repeat", "end repeat", "end repeat",
                  "if con is missing value then set con to current session of current window",
                  "tell con to set s0 to (split horizontally with default profile)"]
    elif where == "tab":
        lines += ["activate", "tell current window to set t to (create tab with default profile)",
                  "set s0 to current session of t"]
    else:
        lines += ["activate", "set w to (create window with default profile)"]
        if bounds:
            lines += [f"set bounds of w to {{{bounds}}}", "delay 0.3"]  # let it resize before splitting
        lines.append("set s0 to current session of w")
    for i in range(columns, len(commands), columns):  # the first split of every later row
        lines.append(f"tell s{i - columns} to set s{i} to (split horizontally with default profile)")
    for i in range(len(commands)):  # then each row, left to right
        if i % columns:
            lines.append(f"tell s{i - 1} to set s{i} to (split vertically with default profile)")
    for i, command in enumerate(commands):
        quoted = command.replace("\\", "\\\\").replace('"', '\\"')
        # Ctrl-U first: wipe anything already typed there (keys meant for another window).
        lines.append(f'tell s{i} to write text (ASCII character 21) & "{quoted}"')
    return "\n".join(lines + ["end tell"])


def open_all_in_iterm(panes: dict[str, str], where: str = "window") -> None:
    agents = [a for a in panes if a != "board"]
    tiles = len(panes) + (where != "here")  # the console is a tile too
    order = tile_order(agents, role_files(), tiles, load_state().get("tiles"))
    order += ["board"] if "board" in panes else []
    for name in order:  # one at a time, here -- the terminals below then only have to look
        own_window(name, panes[name])
    retile()
    me = shlex.quote(str(REPO / "bin" / "oratorio"))
    commands = [f"{me} open {shlex.quote(name)} -w {shlex.quote(WORKSPACE)}" for name in order]
    if where != "here":  # there, the terminal the command was typed in is the console
        # A plain shell in the working folder, for `oratorio add ... --here` and the like.
        commands.append(f"cd {shlex.quote(load_state()['workdir'])} && clear")
        order.append("console")
    # iTerm2 names each split in ITERM_SESSION_ID, as "w0t0p0:<id>".
    session_id = os.environ.get("ITERM_SESSION_ID", "").rpartition(":")[2]
    script = iterm_script(commands, where, screen_bounds() if where == "window" else None, session_id)
    done = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if done.returncode != 0:
        sys.exit(f"Could not drive iTerm2 ({done.stderr.strip()}). Open each one yourself, in a "
                 f"terminal of its own: oratorio open <{'|'.join(order)}>")
    place = {"window": "in a new iTerm2 window", "tab": "in a new iTerm2 tab", "here": "under this terminal"}
    print(f"Opened {place[where]}: {', '.join(order)}")


def cmd_open(args) -> None:
    """Show one agent (or the board view) alone in this terminal. It keeps
    running in tmux, where its listener can reach it; this terminal is
    only a window onto it, so closing the terminal closes nothing."""
    pick_running_workspace(args)
    if not session_exists():
        sys.exit(f"Workspace '{WORKSPACE}' is not running.")
    panes = openable()
    if "board" not in panes:  # it was closed, or stopped: bring it back
        start_board()
        panes = openable()
    if args.tab and args.here:
        sys.exit("Use --tab or --here, not both.")
    if args.all:
        return open_all_in_iterm(panes, "tab" if args.tab else "here" if args.here else "window")
    if args.tab or args.here:
        sys.exit("--tab and --here go with --all. For one agent, make the tab or split yourself "
                 "and run `oratorio open <agent>` in it.")
    if not args.agent:
        sys.exit(f"Say which one to open ({', '.join(panes)}), or --all.")
    if os.environ.get("TMUX"):
        sys.exit("This terminal is already showing tmux. Run `oratorio open` in a plain terminal "
                 "(in iTerm2: a new split or tab).")
    pane = panes.get(args.agent)
    if pane is None:
        sys.exit(f"'{args.agent}' is not running in '{WORKSPACE}'. You can open: {', '.join(panes)}")

    was_tiled = int(tmux("display-message", "-p", "-t", pane, "#{window_panes}")) > 1
    window = own_window(args.agent, pane)
    if was_tiled:
        retile()
    view = view_session(args.agent)
    close_views(args.agent)  # one terminal per agent; a second `open` takes it over
    pass_modified_keys()  # before this terminal attaches: it is asked for them as it does
    os.execvp("tmux", ["tmux", "new-session", "-t", SESSION, "-s", view, ";",
                       "set-option", "-t", view, "destroy-unattached", "on", ";",
                       "set-option", "-t", view, "status", "off", ";",
                       "set-option", "-t", view, "mouse", "on", ";",  # the wheel scrolls back, as in the tiles
                       # The terminal's own title (tab, or iTerm2's bar over each split).
                       "set-option", "-t", view, "set-titles", "on", ";",
                       "set-option", "-t", view, "set-titles-string",
                       f"{args.agent if args.agent == 'board' else agent_label(args.agent)} - {WORKSPACE}", ";",
                       "select-window", "-t", f"{view}:{window}"])


def stop_workspace() -> None:
    close_views()  # they share the workspace's windows, which would otherwise outlive it
    if session_exists():
        tmux("kill-session", "-t", SESSION)
        print(f"Stopped '{WORKSPACE}'.")
    if RUN_DIR.exists():  # tokens in these files are dead weight once the sessions are gone
        for f in RUN_DIR.iterdir():
            f.unlink()
        RUN_DIR.rmdir()


def cmd_down(args) -> None:
    if args.all:
        running = running_workspaces()
        if not running:
            print("Nothing is running.")
        for name in running:
            use_workspace(name)
            stop_workspace()
        return
    pick_running_workspace(args)
    if not session_exists():
        print(f"Workspace '{WORKSPACE}' is not running.")
    stop_workspace()


def main() -> None:
    parser = argparse.ArgumentParser(prog="oratorio", description="Run teams of agents in tiled tmux windows.")
    sub = parser.add_subparsers(dest="command", required=True)
    which = argparse.ArgumentParser(add_help=False)
    which.add_argument("-w", "--workspace", metavar="NAME", help="which workspace (see the shortcuts in --help of `up`)")

    p = sub.add_parser("up", parents=[which], help="start a workspace",
                       description="Start a workspace. Without -w: the only workspace file in the folder, "
                                   "or, with none, a workspace named after the folder.")
    p.add_argument("--workdir", metavar="DIR", help="folder to look in for workspace files (default: current)")
    p.add_argument("--only", help="comma-separated agents to start (default: the workspace file, else one per role)")
    p.add_argument("--model", help="one model for every agent on this run (default: what the "
                                   "workspace file says, else each role's own)")
    p.add_argument("--runner", choices=RUNNERS, help="one runner for every agent on this run (default: what "
                                                      "the workspace file says, else each role's own, else claude)")
    p.add_argument("--permission-mode",
                   help=f"Claude Code permission mode for the agents; Codex agents get the nearest Codex "
                        f"options (default: {DEFAULT_PERMISSION_MODE})")
    p.set_defaults(run=cmd_up)

    p = sub.add_parser("add", parents=[which], help="add one agent to a running workspace")
    p.add_argument("agent", help="a role (experiment -> next free experiment-N) or an exact agent name")
    p.add_argument("--model", help="model for this agent only (default: what the workspace "
                                   "or its role says)")
    p.add_argument("--runner", choices=RUNNERS, help="run this agent in claude or codex (default: what "
                                                      "the workspace or its role says, else claude)")
    p.add_argument("--here", action="store_true",
                   help="iTerm2 only: also show it in a new split under the terminal you type this in")
    p.set_defaults(run=cmd_add)

    p = sub.add_parser("restart", parents=[which], help="start an agent afresh, where it is",
                       description="Stop an agent and start it again in the same tile or terminal, with an "
                                   "empty conversation and the current role file, model and settings. "
                                   "Messages it had not finished are handed to it again.")
    p.add_argument("agent", nargs="?", help="an agent name, or `board` for the board view")
    p.add_argument("--all", action="store_true", help="restart every agent in the workspace")
    p.add_argument("--model", help="also change its model")
    p.add_argument("--runner", choices=RUNNERS, help="also change what it runs in: claude or codex")
    p.set_defaults(run=cmd_restart)

    p = sub.add_parser("remove", parents=[which], help="close one agent")
    p.add_argument("agent")
    p.set_defaults(run=cmd_remove)

    p = sub.add_parser("tile", parents=[which], help="arrange the tiles",
                       description="Put the agents' tiles in the order given, filling the grid row by row; "
                                   "agents you leave out follow. With no agents: back to the automatic order.")
    p.add_argument("agents", nargs="*", help="agent names, in the order you want their tiles")
    p.set_defaults(run=cmd_tile)

    sub.add_parser("save", parents=[which], help="write the running team to its yaml file").set_defaults(run=cmd_save)
    sub.add_parser("attach", parents=[which], help="open a workspace's tiles").set_defaults(run=cmd_attach)
    p = sub.add_parser("open", parents=[which], help="show one agent alone in this terminal",
                       description="Show one running agent, or `board`, alone in this terminal -- for "
                                   "arranging agents yourself in your terminal's own splits or tabs. The "
                                   "agent leaves the tiles and keeps running if you close the terminal; "
                                   "run this again to get it back.")
    p.add_argument("agent", nargs="?", help="an agent name, or `board`")
    p.add_argument("--all", action="store_true",
                   help="iTerm2 only: open a new window with a split for every agent and the board")
    p.add_argument("--tab", action="store_true",
                   help="with --all: a new tab in the iTerm2 window in front, instead of a new window")
    p.add_argument("--here", action="store_true",
                   help="with --all: splits under the terminal you type this in, which stays on top "
                        "as your console")
    p.set_defaults(run=cmd_open)
    sub.add_parser("status", help="every running workspace").set_defaults(run=cmd_status)
    p = sub.add_parser("down", parents=[which], help="stop a workspace")
    p.add_argument("--all", action="store_true", help="stop every running workspace")
    p.set_defaults(run=cmd_down)

    args = parser.parse_args()
    load_dotenv()
    args.run(args)


if __name__ == "__main__":
    main()
