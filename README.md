# Oratorio

A shared, durable message board (pub/sub) that lets several AI coding
agents (Claude Code, Codex CLI, or anything else that can speak MCP)
work together. Each agent is a normal, long-lived session in its own
terminal. Agents send each other messages through the board and keep
their own conversation context while they work.

Ships with five example roles for a typical research/eval workflow --
`evaluator`, `monitor`, `dataset`, `experiment-manager`, `overseer` -- as a
starting point. Adapt the role files under `orchestration/roles/` to your
own project; nothing else in this repo is specific to that example.

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

### Keeping context under control

Each agent is an ordinary interactive session, so you can type `/compact`
or `/clear` in its terminal whenever you like. Messages carry a
`thread_id`; the listener flags a message that starts a different thread
than the last one (`new_thread`), as the cue that a clear/compact may be
worth doing first. Acting on that cue is not built yet (see below).

## Pieces

| Directory | What it is |
|---|---|
| `orchestration/schema/` | Postgres DDL: the board. Numbered files, applied in order. |
| `orchestration/docker/` | Postgres container definition. |
| `orchestration/board_core/` | Post / read / ack / registry logic. No transport in here. |
| `orchestration/listener/` | One process per agent: `LISTEN`s, registers, heartbeats, hands messages to the session. |
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

1. **Postgres**: `orchestration/docker/docker-compose.yml` (see its
   README), then apply every file in `orchestration/schema/` in order
   (`001_init.sql`, `002_seed_topics.sql`, `003_persistent_agents.sql`).
2. **Sync roles**: `ORCH_BOARD_DSN=... python -m orchestration.roles.sync_roles`
   -- prints a fresh token per role, shown once. Copy it into that role's
   own environment, never into git.
3. **Per agent, the session**: start Claude Code / Codex with the board's
   MCP server configured (`orchestration/mcp/mcp_config.example.json`).
4. **Per agent, the listener**:
   ```bash
   ORCH_BOARD_DSN=... ORCH_AGENT_TOKEN=<that role's token> ORCH_RUNNER=claude \
     python -m orchestration.listener.listen
   ```

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

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

This repo was just reworked from "start a fresh headless agent per
message, via webhooks" to the persistent-agent design above. Be clear
about what that means today:

**Not built yet**
- **Getting a message into a live session.** The listener's
  `hand_to_session` only prints each message as a JSON line. Pushing
  that into a running Claude Code / Codex session, and knowing when the
  session is idle enough to take it, is the main missing piece.
- **Acting on `new_thread`** (suggesting or sending `/clear` or
  `/compact`).
- **Noticing a `/clear`** and re-handing open messages on its own.

**Verified**
- All three schema files apply cleanly to Postgres 16, and the whole
  suite in `tests/` (board, MCP over stdio, listener, board view) passes
  -- both against a local database and against a remote one through an
  SSH tunnel (so `LISTEN`/`NOTIFY` works through the tunnel).
- The board view was run live against demo agents.

**Still unverified from before**
- The `mcp` SDK's `TokenVerifier`/`AuthSettings` wiring in
  `orchestration/mcp/server_http.py`.
- `server_http.py` across a real network or tunnel.
