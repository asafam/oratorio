#!/usr/bin/env python3
"""Start the whole team in one tiled tmux window: one pane per agent, plus
the board view. Each agent is an ordinary interactive Claude Code session
you can watch and type in; a listener per agent (second tmux window) types
board messages into its pane whenever it is idle.

    python -m orchestration.session.up [--workdir DIR] [--only a,b] [--model M]
    tmux attach -t oratorio
    python -m orchestration.session.up --down

Agents come from orchestration/roles/*.md. On every start each agent gets
a fresh token (the previous one stops working), so nothing secret needs
to be kept between runs; the per-run files live in .oratorio/ (gitignored).

Nothing here spends tokens on its own: sessions start idle and only work
when a message, or you, gives them something to do.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from orchestration.board_core import auth, db, registry  # noqa: E402
from orchestration.roles.sync_roles import ROLES_DIR, git_blob_sha, parse_role_file  # noqa: E402

SESSION = "oratorio"
RUN_DIR = REPO / ".oratorio"
MCP_SERVER_NAME = "orchestration-board"

PROTOCOL = """\
# You are `{agent_id}`, one agent in a team that works through a shared message board

Your teammates: {peers}. You never talk to them directly -- only through
the board tools (`{mcp}`), and they answer the same way.

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
- `list_agents` shows who is online. `read_messages` shows what is still
  open for you -- check it if your context was cleared or compacted.

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


def sync_agent(conn, path: Path) -> tuple[dict, str, str]:
    """Load one role file into the board and give it a fresh token.
    Returns (frontmatter, brief, token)."""
    meta, brief = parse_role_file(path)
    topics = meta.get("topics", [])
    for topic in topics:  # a role may name a topic nobody has seeded yet
        conn.execute(
            "INSERT INTO board.topic (topic) VALUES (%s) ON CONFLICT DO NOTHING", (topic,)
        )
    token = auth.generate_token()
    registry.upsert_agent(
        conn,
        agent_id=meta["agent_id"],
        role_doc_path=str(path.relative_to(REPO)),
        role_version=git_blob_sha(path),
        brief=brief,
        peers=meta.get("peers", []),
        topics=topics,
        auth_token_hash=auth.hash_token(token),
        is_auditor=bool(meta.get("is_auditor", False)),
    )
    registry.set_auth_token_hash(conn, meta["agent_id"], auth.hash_token(token))
    return meta, brief, token


def write_private(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o600)


def agent_command(agent_id: str, meta: dict, brief: str, token: str, args) -> tuple[str, Path]:
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
        PROTOCOL.format(agent_id=agent_id, peers=", ".join(meta.get("peers", [])) or "none",
                        mcp=MCP_SERVER_NAME) + brief + "\n"
    )

    command = [
        "claude", "-n", agent_id,
        "--model", args.model or meta.get("model", "sonnet"),
        # Skip user-level plugins/hooks/MCP servers: an agent needs the
        # board and its role, not everything installed on this machine.
        "--setting-sources", "project",
        "--strict-mcp-config", "--mcp-config", str(mcp_config),
        "--settings", str(settings),
        "--append-system-prompt-file", str(prompt),
        "--permission-mode", args.permission_mode,
        "--allowedTools", f"mcp__{MCP_SERVER_NAME}",
    ]
    return shlex.join(command), idle_flag


def listener_command(agent_id: str, token: str, pane: str, idle_flag: Path) -> str:
    env_file = RUN_DIR / f"{agent_id}.env"
    write_private(env_file, "".join(f"{k}={shlex.quote(v)}\n" for k, v in {
        "ORCH_BOARD_DSN": os.environ["ORCH_BOARD_DSN"],
        "ORCH_AGENT_TOKEN": token,
        "ORCH_RUNNER": "claude",
        "ORCH_TMUX_PANE": pane,
        "ORCH_IDLE_FLAG": str(idle_flag),
    }.items()))
    return (f"set -a; . {shlex.quote(str(env_file))}; set +a; "
            f"exec {shlex.quote(sys.executable)} -m orchestration.listener.listen")


def up(args) -> None:
    if session_exists():
        sys.exit(f"tmux session '{SESSION}' is already running. Attach with "
                 f"`tmux attach -t {SESSION}`, or stop it with --down first.")
    if not os.environ.get("ORCH_BOARD_DSN"):
        sys.exit("ORCH_BOARD_DSN is not set (put it in .env, or export it).")

    role_files = sorted(ROLES_DIR.glob("*.md"))
    only = set(args.only.split(",")) if args.only else None
    workdir = str(Path(args.workdir).resolve())
    RUN_DIR.mkdir(exist_ok=True)
    RUN_DIR.chmod(0o700)

    try:
        pool = db.get_pool()
        pool.wait(timeout=10)
    except Exception as e:
        sys.exit(f"Cannot reach the board database ({e}). Is the tunnel up? "
                 "See orchestration/ops/scripts/board_tunnel.sh")

    agents = []  # (agent_id, pane command, idle flag, token)
    with pool.connection() as conn:
        for path in role_files:
            meta, brief, token = sync_agent(conn, path)
            if only and meta["agent_id"] not in only:
                continue
            command, idle_flag = agent_command(meta["agent_id"], meta, brief, token, args)
            agents.append((meta["agent_id"], command, idle_flag, token))
        conn.commit()
    db.close_pool()
    if not agents:
        sys.exit("No agents to start.")

    # Window 1: one tile per agent, plus the board view.
    panes: dict[str, str] = {}
    for i, (agent_id, command, _, _) in enumerate(agents):
        if i == 0:
            pane = tmux("new-session", "-d", "-s", SESSION, "-n", "agents", "-x", "250", "-y", "60",
                        "-c", workdir, "-P", "-F", "#{pane_id}", command)
        else:
            pane = tmux("split-window", "-t", f"{SESSION}:agents", "-c", workdir,
                        "-P", "-F", "#{pane_id}", command)
            tmux("select-layout", "-t", f"{SESSION}:agents", "tiled")
        tmux("select-pane", "-t", pane, "-T", agent_id)
        panes[agent_id] = pane
    board = tmux("split-window", "-t", f"{SESSION}:agents", "-c", str(REPO), "-P", "-F", "#{pane_id}",
                 f"{shlex.quote(sys.executable)} -m orchestration.watch.board")
    tmux("select-pane", "-t", board, "-T", "BOARD")
    tmux("select-layout", "-t", f"{SESSION}:agents", "tiled")
    tmux("set-option", "-t", SESSION, "pane-border-status", "top")
    tmux("set-option", "-t", SESSION, "pane-border-format", " #{pane_title} ")
    tmux("set-option", "-t", SESSION, "mouse", "on")  # click a tile to type in it

    # Window 2: the listeners (plain code; they only show log lines).
    for i, (agent_id, _, idle_flag, token) in enumerate(agents):
        command = listener_command(agent_id, token, panes[agent_id], idle_flag)
        if i == 0:
            pane = tmux("new-window", "-d", "-t", SESSION, "-n", "listeners", "-c", str(REPO),
                        "-P", "-F", "#{pane_id}", command)
        else:
            pane = tmux("split-window", "-d", "-t", f"{SESSION}:listeners", "-c", str(REPO),
                        "-P", "-F", "#{pane_id}", command)
            tmux("select-layout", "-t", f"{SESSION}:listeners", "tiled")
        tmux("select-pane", "-t", pane, "-T", f"listener: {agent_id}")
    tmux("select-pane", "-t", panes[agents[0][0]])

    print(f"Started {len(agents)} agent(s): {', '.join(a[0] for a in agents)}")
    print(f"Working directory: {workdir}")
    print(f"Watch them:  tmux attach -t {SESSION}")
    print("Stop them:   python -m orchestration.session.up --down")


def down() -> None:
    if session_exists():
        tmux("kill-session", "-t", SESSION)
        print("Stopped.")
    else:
        print("Nothing is running.")
    if RUN_DIR.exists():  # tokens in these files are dead weight once the sessions are gone
        for f in RUN_DIR.iterdir():
            f.unlink()


def main() -> None:
    parser = argparse.ArgumentParser(description="Start the agent team in a tiled tmux window.")
    parser.add_argument("--workdir", default=".", help="directory the agents work in (default: current)")
    parser.add_argument("--only", help="comma-separated agent ids to start (default: all roles)")
    parser.add_argument("--model", help="use this model for every agent (default: each role's own)")
    parser.add_argument("--permission-mode", default="acceptEdits",
                        help="Claude Code permission mode for the agents (default: acceptEdits)")
    parser.add_argument("--down", action="store_true", help="stop everything")
    args = parser.parse_args()

    load_dotenv()
    if args.down:
        down()
    else:
        up(args)


if __name__ == "__main__":
    main()
