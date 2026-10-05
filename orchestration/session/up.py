#!/usr/bin/env python3
"""Run a team of agents in one tiled tmux window: one pane per agent, plus
the board view. Each agent is an ordinary interactive Claude Code session
you can watch and type in; a listener per agent (second tmux window) types
board messages into its pane whenever it is idle.

    oratorio up [-w NAME] [--only a,b] [--model M]    start a workspace
    oratorio add <agent> [-w NAME]                     add one while running
    oratorio remove <agent> [-w NAME]                  close one
    oratorio save [-w NAME]                            write its yaml file
    oratorio attach [-w NAME]                          open its tiles
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
    agents: [manager, todo, experiment-1, experiment-2]
    model: sonnet                # optional: one model for every agent
    permission_mode: acceptEdits

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
    if len(here) == 1:
        return use_workspace(here[0])
    if len(running) == 1:
        return use_workspace(running[0])
    sys.exit(f"Several workspaces are running ({', '.join(running)}). Say which one with -w NAME.")
DEFAULT_PERMISSION_MODE = "acceptEdits"
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


def write_private(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o600)


def agent_command(agent_id: str, meta: dict, brief: str, token: str, state: dict) -> tuple[str, Path]:
    """Write this agent's per-run files; return (shell command for its
    pane, its idle-flag path)."""
    idle_flag = RUN_DIR / f"{agent_id}.idle"
    idle_flag.unlink(missing_ok=True)
    Path(str(idle_flag) + ".cleared").unlink(missing_ok=True)

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
    write_private(settings, json.dumps({"hooks": {
        "SessionStart": [hook(f"touch {flag}"),
                         {"matcher": "clear", **hook(f"touch {flag}.cleared")}],
        "UserPromptSubmit": [hook(f"rm -f {flag}")],
        "Stop": [hook(f"touch {flag}")],
    }}, indent=2))

    prompt = RUN_DIR / f"{agent_id}.prompt.md"
    prompt.write_text(
        PROTOCOL.format(agent_id=agent_id, workspace=WORKSPACE, mcp=MCP_SERVER_NAME) + brief + "\n"
    )

    command = [
        "claude", "-n", agent_id,
        "--model", state.get("model") or meta.get("model", "sonnet"),
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


def retile() -> None:
    for window in ("agents", "listeners"):
        if window_exists(window):
            tmux("select-layout", "-t", f"{SESSION}:{window}", "tiled")


def start_agent(conn, agent_id: str, path: Path, state: dict) -> None:
    """Everything for one agent: board row + fresh token, its session
    pane, and its listener pane. Creates the tmux session/windows if this
    is the first one."""
    meta, brief, token = sync_agent(conn, agent_id, path)
    conn.commit()
    command, idle_flag = agent_command(agent_id, meta, brief, token, state)
    workdir = state["workdir"]

    if not session_exists():
        pane = tmux("new-session", "-d", "-s", SESSION, "-n", "agents", "-x", "250", "-y", "60",
                    "-c", workdir, "-P", "-F", "#{pane_id}", command)
        tmux("set-option", "-t", SESSION, "pane-border-status", "top")
        tmux("set-option", "-t", SESSION, "pane-border-format", " #{pane_title} ")
        tmux("set-option", "-t", SESSION, "mouse", "on")  # click a tile to type in it
    else:
        pane = tmux("split-window", "-d", "-t", f"{SESSION}:agents", "-c", workdir,
                    "-P", "-F", "#{pane_id}", command)
    tmux("select-pane", "-t", pane, "-T", agent_id)
    retile()

    env = {
        "ORCH_BOARD_DSN": os.environ["ORCH_BOARD_DSN"],
        "ORCH_AGENT_TOKEN": token,
        "ORCH_RUNNER": "claude",
        "ORCH_TMUX_PANE": pane,
        "ORCH_IDLE_FLAG": str(idle_flag),
    }
    env_file = RUN_DIR / f"{agent_id}.env"
    write_private(env_file, "".join(f"{k}={shlex.quote(v)}\n" for k, v in env.items()))
    listener = (f"set -a; . {shlex.quote(str(env_file))}; set +a; "
                f"exec {shlex.quote(sys.executable)} -m orchestration.listener.listen")
    # The listeners live in a second window (plain code; they only show log lines).
    if not window_exists("listeners"):
        listener_pane = tmux("new-window", "-d", "-t", SESSION, "-n", "listeners", "-c", str(REPO),
                             "-P", "-F", "#{pane_id}", listener)
    else:
        listener_pane = tmux("split-window", "-d", "-t", f"{SESSION}:listeners", "-c", str(REPO),
                             "-P", "-F", "#{pane_id}", listener)
    tmux("select-pane", "-t", listener_pane, "-T", f"listener: {agent_id}")
    with env_file.open("a") as f:
        f.write(f"ORCH_LISTENER_PANE={shlex.quote(listener_pane)}\n")
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
    # Command line wins over the workspace file, which wins over "one per role".
    names = args.only.split(",") if args.only else workspace.get("agents") or list(roles)
    wanted: dict[str, str] = {}  # agent name -> role
    for entry in names:
        try:
            agent_id, role = resolve_agent(entry, roles, set(wanted))
        except ValueError as e:
            sys.exit(str(e))
        wanted[agent_id] = role

    state = {
        "name": name,
        "workdir": str(workdir),
        "file": str(file) if file else None,
        "model": args.model or workspace.get("model"),
        "permission_mode": args.permission_mode or workspace.get("permission_mode")
                           or DEFAULT_PERMISSION_MODE,
    }
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

    board = tmux("split-window", "-d", "-t", f"{SESSION}:agents", "-c", str(REPO), "-P", "-F",
                 "#{pane_id}",
                 f"{shlex.quote(sys.executable)} -m orchestration.watch.board -w {shlex.quote(name)}")
    tmux("select-pane", "-t", board, "-T", f"BOARD: {name}")
    retile()

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
    if args.model:
        state = {**state, "model": args.model}
    pool = connect()
    with pool.connection() as conn:
        start_agent(conn, agent_id, roles[role], state)
    db.close_pool()
    print(f"Added {agent_id} to '{WORKSPACE}'.")


def cmd_remove(args) -> None:
    pick_running_workspace(args)
    record = running_agents().get(args.agent)
    if record is None:
        sys.exit(f"'{args.agent}' is not running in '{WORKSPACE}'.")
    for key in ("ORCH_LISTENER_PANE", "ORCH_TMUX_PANE"):
        if record.get(key):
            subprocess.run(["tmux", "kill-pane", "-t", record[key]], capture_output=True)
    forget_agent(args.agent)
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

    workspace = {
        "name": WORKSPACE,
        "workdir": os.path.relpath(workdir, target.parent),
        "agents": sorted(agents, key=_team_order(role_files())),
    }
    if state.get("model"):
        workspace["model"] = state["model"]
    workspace["permission_mode"] = state["permission_mode"]
    target.write_text(
        f"# Oratorio workspace. Start it from this folder with: oratorio up -w {WORKSPACE}\n"
        + yaml.safe_dump(workspace, sort_keys=False, default_flow_style=None)
    )
    STATE_FILE.write_text(json.dumps({**state, "file": str(target)}, indent=2))
    print(f"Saved '{WORKSPACE}' ({len(agents)} agent(s)) to {target}")


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


def stop_workspace() -> None:
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
    p.add_argument("--model", help="one model for every agent (default: each role's own)")
    p.add_argument("--permission-mode",
                   help=f"Claude Code permission mode for the agents (default: {DEFAULT_PERMISSION_MODE})")
    p.set_defaults(run=cmd_up)

    p = sub.add_parser("add", parents=[which], help="add one agent to a running workspace")
    p.add_argument("agent", help="a role (experiment -> next free experiment-N) or an exact agent name")
    p.add_argument("--model", help="model for this agent (default: same as the rest)")
    p.set_defaults(run=cmd_add)

    p = sub.add_parser("remove", parents=[which], help="close one agent")
    p.add_argument("agent")
    p.set_defaults(run=cmd_remove)

    sub.add_parser("save", parents=[which], help="write the running team to its yaml file").set_defaults(run=cmd_save)
    sub.add_parser("attach", parents=[which], help="open a workspace's tiles").set_defaults(run=cmd_attach)
    sub.add_parser("status", help="every running workspace").set_defaults(run=cmd_status)
    p = sub.add_parser("down", parents=[which], help="stop a workspace")
    p.add_argument("--all", action="store_true", help="stop every running workspace")
    p.set_defaults(run=cmd_down)

    args = parser.parse_args()
    load_dotenv()
    args.run(args)


if __name__ == "__main__":
    main()
