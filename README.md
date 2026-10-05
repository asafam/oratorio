# Oratorio

A shared, durable message board (pub/sub) that lets several AI coding
agents (Claude Code, Codex CLI, or anything else that can speak MCP)
work together. Each agent is a normal, long-lived session in its own
terminal. Agents send each other messages through the board and keep
their own conversation context while they work.

Ships with a small research team as a starting point -- `manager`,
`experiment` (as many as you want), `reviewer`, `writer` -- and one
command that opens them as tiles in a single terminal window, next to a
live view of what they are saying to each other. Adapt the role files
under `orchestration/roles/` to your own project; nothing else in this
repo is specific to that team.

## Quick start

```bash
orchestration/ops/scripts/board_tunnel.sh up     # only if the board is remote
cd ~/my-research
oratorio up                                      # bin/oratorio in this repo
oratorio attach
```

You get one tile per agent plus a `BOARD` tile, laid out in a grid for
you. Click a tile to type in it. Give your goal to `manager`; it hands
work to the others through the board.

| Command | What it does |
|---|---|
| `oratorio up` | Starts a workspace. |
| `oratorio attach` | Opens its tiles. |
| `oratorio add experiment` | Adds an agent as a new tile. Roles that can have several get a running number: `experiment-1`, then `experiment-2`, ... With `--here` (iTerm2) it also appears in a new split under the terminal you typed it in. |
| `oratorio restart experiment-2` | Starts that agent afresh in the same tile or terminal: an empty conversation, and the current role file, model and settings. Messages it had not finished are handed to it again. `--all` restarts every agent; `--model opus` also changes its model. |
| `oratorio remove experiment-2` | Closes that agent. Messages sent to it wait until it is added again. |
| `oratorio open manager` | Shows that one agent (or `board`) alone in the terminal you type it in, so you can arrange agents yourself in your terminal's own splits or tabs. Closing the terminal closes nothing; run it again to get the agent back. |
| `oratorio open --all` | iTerm2 only: opens a new window with a split for every agent and the board, plus a plain console in the working folder for typing commands. Each split is labelled with the agent and its model, `manager (fable)`. Add `--tab` for a new tab in the current window instead, or `--here` to put the splits under the terminal you typed it in, which stays on top as your console. |
| `oratorio tile manager experiment-1` | Arranges the tiles in that order, row by row; agents you leave out follow. With no names: the automatic order, which keeps numbered agents side by side. Also brings back any agent you opened on its own. |
| `oratorio save` | Writes the running team to the workspace's yaml file. |
| `oratorio status` | Every running workspace, and what can be added to each. |
| `oratorio down` | Stops a workspace (`--all` for every one). |

### Workspaces

A workspace is a **name plus a team**. Several can run at the same time.
On the board they are fully separate: `manager` in one workspace and
`manager` in another are different agents, and neither can see or reach
the other's messages. Each workspace gets its own set of tiles.

A workspace is described by a small yaml file:

```yaml
name: thesis
workdir: .                     # where its agents work; default: this file's folder
agents:
  manager:
    model: opus                # optional: a model for this agent
  experiment-1:
  experiment-2:
model: sonnet                  # optional: one model for every other agent
permission_mode: auto           # optional; this is the default
```

Call the file `oratorio.yaml`, or `<anything>.oratorio.yaml` to keep
several workspaces in one folder. Write it by hand, or build a team with
`add`/`remove` and run `oratorio save`.

`-w NAME` on any command says which workspace you mean:

```bash
oratorio up -w thesis
oratorio up -w ablations
oratorio add experiment -w ablations
oratorio status
```

Leaving `-w` out is a shortcut, not a rule:
- `up` uses the folder's only workspace file; with no file at all, it
  names the workspace after the folder and starts one agent per role.
- The other commands use the workspace of the folder you are in if it is
  running, or the only one that is running.

A folder is not tied to one workspace, and a workspace is not tied to
the folder its file sits in (`workdir`). Two teams working in the same
folder at once can overwrite each other's files (both `manager` agents
write `TODO.md`, for example) -- nothing stops you, so point them at
different folders unless you mean it. Two running workspaces cannot
share a name.

More options for `up`: `--only manager,reviewer`, `--model haiku`,
`--permission-mode MODE`, `--workdir DIR` (look there for workspace
files instead of the current folder).

- `permission_mode` is how much the agents may do without asking you
  (Claude Code's own modes). The default, `auto`, lets an agent go ahead
  with what it judges safe and stop to ask only for the rest, so the team
  is not left waiting while you are away. For a tighter rein use
  `acceptEdits` (edits files freely, asks before most commands).
- Agents do not load your personal Claude Code plugins, hooks or MCP
  servers. They do take over your auto mode settings from
  `~/.claude/settings.json`, and a role marked `status_line: true`
  (`manager`, as shipped) also shows your status line.
- The first time agents start in a new folder, each tile asks whether you
  trust that folder.
- The listeners run in a second tmux window (`Ctrl-b n` to see it).
- In iTerm2, `tmux -CC attach -t oratorio-<name>` shows the tiles as
  native iTerm2 splits.

### Roles

Each file in `orchestration/roles/` is one role: a few settings on top
(`model`, `topics`, `status_line`, ...) and a plain-language brief below. `multiple:
true` lets a role run as several numbered agents; `{agent_id}` in the
brief is replaced with each agent's own id.

## How it works

```
 agent session  --MCP: post / ack-->   BOARD (Postgres)
 (Claude/Codex)                        topics, inboxes, who is online
       ^                                     |
       |  hands the message in               |  NOTIFY ("something new")
       +----------  listener  <--------------+
                    (plain code, no tokens)
```

- **The board** is the bus. Messages go to one agent, to a topic (every
  subscriber gets it), or to everyone (broadcast). Each recipient gets
  its own inbox row, saved in the same transaction as the message.
- **A listener** runs beside each agent session. It waits on Postgres
  `LISTEN` -- plain code, so waiting costs no tokens -- and hands each new
  message to the session. The model only runs when there is real work.
- **MCP** is how the agent talks back: post a message, mark one done,
  see who is online. It is the "publish" side of the same bus, not a
  second message system.
- **The board owns coordination.** Claude and Codex are just workers
  behind the same interface; swapping one for the other changes nothing
  on the board.

### An agent can be down for hours

Nothing is lost. A message waits in the recipient's inbox until that
agent has **acked** it (said "done"). Reading a message does not consume
it, and neither does a session that crashes or gets cleared mid-task.
When the listener is restarted, anything the old session was handed but
never acked is handed in again. So delivery is at-least-once: an agent
may see the same message id twice.

The listener cannot tell that you typed `/clear` in a session. After a
clear, either restart that agent's listener or have the agent call
`read_messages` -- both show what is still open.

### Wait or not -- the sender decides, per message

| Set on the message | Meaning |
|---|---|
| nothing | Wait in the inbox as long as it takes (default). |
| `expires_in_seconds` | Only matters now. Dropped if not handled by then. |
| `reply_within_seconds` | "I need an answer by then." If none arrives, the sender is told once and decides what to do. |

### Registering

Two separate steps, on purpose:

1. **Identity (once, by you).** `sync_roles.py` creates the agent and
   prints its token. An agent cannot create itself: it needs a token
   before the board will talk to it at all.
2. **"I'm up" (every start, by the agent).** Its listener registers it:
   what it is (`claude`, `codex`, ...) and that it is online, then keeps
   a heartbeat going. `list_agents` shows who is online. Agents only
   ever know the board, never each other's addresses.

### Getting a message into a live session

The listener types it into the agent's tmux pane, the way you would.
Every board line starts with `[board message <id> from <sender>]`, so the
agent can tell it from you typing. This needs nothing from the agent
tool except a terminal, so it is the same for Claude Code and Codex.

It never types while the agent is busy. The agent's own hooks keep an
"idle" flag file: there while it waits for input, gone while it works.
The listener hands over one message, then waits for idle before the next.

Known limit: if you have half-typed text sitting in an idle tile when a
board message arrives, the message is typed on top of it.

### Keeping context under control

Each agent is an ordinary interactive session, so you can type `/compact`
or `/clear` in its tile whenever you like. Three things keep that from
being needed often, or from losing work:

- Role files tell agents to push heavy work into subagents and keep only
  the summary.
- Messages carry a `thread_id`. When a message starts a different thread
  than the previous one, a note is typed with it saying this is a good
  moment for `/compact` or `/clear`. Nothing outside the session can run
  those commands -- the note is for you.
- After a `/clear`, the listener notices and hands back every message the
  agent had not finished.

## Pieces

| Directory | What it is |
|---|---|
| `orchestration/schema/` | Postgres DDL: the board. Numbered files, applied in order. |
| `orchestration/docker/` | Postgres container definition. |
| `orchestration/board_core/` | Post / read / ack / registry logic. No transport in here. |
| `orchestration/listener/` | One process per agent: `LISTEN`s, registers, heartbeats, hands messages to the session. |
| `orchestration/session/` | `up.py` runs the team in tmux (the `oratorio` command); `tmux.py` types messages into a pane. |
| `orchestration/mcp/` | MCP server exposing the board as tools. `server.py` (stdio, agent can reach Postgres directly) / `server_http.py` (agent on another machine). |
| `orchestration/watch/` | The board view: a live feed of every message, for you to watch. |
| `orchestration/roles/` | One Markdown+frontmatter file per role, plus the sync script. |
| `orchestration/ops/systemd/` | Optional service files (listener, tunnel for `server_http.py`). |

## Watching the agents

Run the board view in its own terminal and leave it open:

```bash
ORCH_BOARD_DSN=... python -m orchestration.watch.board
```

It shows who is online, each message as it is posted, and when each
recipient is handed it and finishes it:

```
● evaluator  online (claude)
○ monitor  offline, last seen 12:49:09
------------------------------------------------------------
12:49:09  41  evaluator -> #eval-results  [thread run-12]
          Run 12 done: accuracy 0.83, 2 failures
          to: dataset, monitor
12:49:09  41  handed to dataset
12:49:10  41  dataset finished it
```

With `-w NAME` it shows one workspace; without, every workspace on the
board, with agents shown as `workspace/name`.

It only reads -- watching never marks a message as seen. It checks the
database once a second, which is plain code and costs no tokens.
`--once` prints the current state and exits; `--history N` sets how many
recent messages to show first.

## Where things run

The simple setup is **everything on one machine**: Postgres, and one
terminal + one listener per agent.

If agents run on other machines, each one needs two things:
- its **listener** must reach Postgres itself (`LISTEN` is a database
  feature -- the MCP endpoint alone is not enough), so the database has
  to be reachable from that machine (private network, VPN, SSH tunnel);
- its **session** reaches the board through `server_http.py` or, if it
  has that same database access, plain `server.py`.

Keep the board on a small, always-on host you control, not on a shared
machine that gets rebooted without warning -- everything else depends on
it being up.

### Board on a remote server, through an SSH tunnel

If you don't want Postgres on your own machine, put it on a small server
and reach it through an SSH tunnel. Postgres listens only on that
server's `localhost`, so it is never exposed to the internet, and both
the listener and the MCP server use it as if it were local.

On the server (Ubuntu shown):

```bash
apt install postgresql
sudo -u postgres psql -c "CREATE ROLE orchestration LOGIN PASSWORD '<password>'"
sudo -u postgres createdb -O orchestration orchestration_board
```

Some networks only let web ports out. If SSH on port 22 is blocked from
where you work, make the server's sshd also listen on 443 (only on a
server that isn't already serving a website there):

```bash
printf 'Port 22\nPort 443\n' > /etc/ssh/sshd_config.d/10-oratorio-ports.conf
systemctl daemon-reload && systemctl restart ssh.socket
```

On your machine:

```bash
export ORATORIO_BOARD_HOST=<server address>
orchestration/ops/scripts/board_tunnel.sh up      # also: down, status
export ORCH_BOARD_DSN=postgresql://orchestration:<password>@localhost:5433/orchestration_board
```

Keep those values in a `.env` file (gitignored), never in git.

**Moving the board** somewhere else is a dump and a restore, then
pointing `ORATORIO_BOARD_HOST` at the new place:

```bash
pg_dump "$ORCH_BOARD_DSN" > board.sql        # everything, one file
psql "$NEW_ORCH_BOARD_DSN" -f board.sql      # into an empty database
```

## Bring-up order

1. **Postgres**: locally with `orchestration/docker/docker-compose.yml`
   (see its README), or on a remote server (above). Then apply every
   file in `orchestration/schema/` in order (`001_init.sql`,
   `002_seed_topics.sql`, `003_persistent_agents.sql`,
   `004_workspaces.sql`).
2. **Point at it**: put `ORCH_BOARD_DSN=...` in a `.env` file in the repo
   root (gitignored).
3. **Start the team**: `bin/oratorio up` (see Quick start). It loads
   the role files into the board, gives each agent a fresh token, and
   starts the sessions, listeners and board view. To type plain
   `oratorio` from any folder, see Setup below.

To run one agent by hand instead (another machine, another tool):
`python -m orchestration.roles.sync_roles [--workspace NAME]` prints its
token once; start
the session with the board's MCP server configured
(`orchestration/mcp/mcp_config.example.json`), and run
`python -m orchestration.listener.listen` with `ORCH_BOARD_DSN`,
`ORCH_AGENT_TOKEN`, and -- to have messages typed in -- `ORCH_TMUX_PANE`
and `ORCH_IDLE_FLAG`.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### The `oratorio` command

`bin/oratorio` is a small script that runs the launcher with this repo's
own `.venv`, so it works from any folder. To call it as plain `oratorio`,
link it into a folder that is on your `PATH` -- run this from the repo
root:

```bash
mkdir -p ~/.local/bin
ln -s "$PWD/bin/oratorio" ~/.local/bin/oratorio
```

If `oratorio` is then "command not found", `~/.local/bin` is not on your
`PATH` yet. Add this line to `~/.zshrc` (or `~/.bashrc`) and open a new
terminal:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

It is a link, not a copy: pulling new code updates the command, and
moving the repo breaks it (make the link again). To remove it:
`rm ~/.local/bin/oratorio`.

## Running the tests

The suite needs a real Postgres reachable at `ORCH_BOARD_DSN` with the
schema applied (a throwaway dev instance is fine -- see
`orchestration/docker/README.md`). Tests are skipped automatically if
`ORCH_BOARD_DSN` isn't set.

```bash
export ORCH_BOARD_DSN=postgresql://orchestration:<password>@localhost:5433/orchestration_board
pytest tests/ -v
```

## Status

**Verified**
- All four schema files apply cleanly to Postgres 16, and the whole
  suite in `tests/` passes -- against a local database and against a
  remote one through an SSH tunnel (so `LISTEN`/`NOTIFY` works through
  the tunnel).
- Live, with real Claude Code sessions started by `session/up.py`: a
  request typed to `manager` went to `todo` through the board, `todo`
  did the work and replied, the reply was typed back to `manager`, and
  both acked. After a `/clear`, the unfinished message was handed back.

- Live: adding and removing agents while others keep working, numbered
  experiment agents, saving a workspace and starting again from it.

- Workspace separation, two ways: tests that try every route across
  (by name, by internal key, by topic, broadcast, reply, raw SQL, the
  audit view) and are refused; and live, with two workspaces of the same
  agents running in one folder -- a request in one never showed up in
  the other.

**Not tried yet**
- Message traffic with more than two agents at once in one workspace.
- Each agent of a workspace working in its own folder -- all agents of a
  workspace share its `workdir`.

**Not built yet**
- **Codex.** The typing mechanism does not care which tool is in the
  pane, but the idle flag is set by Claude Code hooks, and `up.py` only
  knows how to start Claude Code.
- **Protecting a half-typed human draft** in an idle tile (see above).

**Still unverified from before**
- The `mcp` SDK's `TokenVerifier`/`AuthSettings` wiring in
  `orchestration/mcp/server_http.py`.
- `server_http.py` across a real network or tunnel. (The team started
  by `up.py` uses the stdio server, which is tested.)
