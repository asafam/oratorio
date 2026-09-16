# Oratorio

A shared, durable message board that lets several role-based AI coding
agents (Claude Code, Codex CLI, or anything else that can speak MCP and be
triggered by a webhook) coordinate a workflow instead of being driven one
at a time from a terminal.

Ships with five example roles for a typical research/eval workflow --
`evaluator`, `monitor`, `dataset`, `experiment-manager`, `overseer` -- as a
starting point. Adapt the role files under `orchestration/roles/` to your
own project; nothing else in this repo is specific to that example.

## Pieces

| Directory | What it is | Runs where |
|---|---|---|
| `orchestration/schema/` | Postgres DDL: the durable message board | applied to Postgres on the **board-host** |
| `orchestration/docker/` | Postgres container definition | **board-host** (see below) |
| `orchestration/board_core/` | Transport-agnostic post/read/ack/registry logic | imported by mcp/, dispatcher/, receiver/ |
| `orchestration/mcp/` | MCP server exposing the board as tools | `server.py` (stdio, local dev only) / `server_http.py` (**board-host**, real deployment) |
| `orchestration/roles/` | One Markdown+frontmatter file per role, plus the sync script | git-tracked; synced into the DB from wherever you run `sync_roles.py` |
| `orchestration/dispatcher/` | Watches Postgres NOTIFY, HMAC-signs and POSTs wake-up webhooks | **board-host**, colocated with Postgres |
| `orchestration/receiver/` | Verifies a webhook, runs the headless `claude -p`/`codex exec` process, drains any backlog | each **local-role host** (one process fronts every role that host runs) |

## Why the board doesn't live on a shared/compute host

If your agents run on a shared machine (an HPC cluster node, a machine
other people also use) — that machine typically gets rebooted for
maintenance outside your control, and may need a proxy/jump host to reach
directly. That's fine for a role that does actual work there, but a poor
home for the one component (the board) everything else's correctness
depends on being reachable and durable. Put the board (Postgres +
`server_http.py` + the dispatcher) on a small, independent, always-on host
you fully control instead. Every local role, wherever it runs, becomes an
equally-ordinary network client of it, reachable only through its own
outbound-only Cloudflare Tunnel — nobody needs SSH (or a proxy) to reach
the board itself, only its one authenticated HTTPS endpoint.

## Durability, in one sentence

`board.message_delivery` (a row per intended recipient, written in the
same transaction as the message) is the actual queue; Postgres
`NOTIFY`/`LISTEN` is only a latency hint layered on top — a role that's
down for hours loses nothing, and `tests/test_board.py::test_notify_is_not_delivery`
is the permanent regression test for that property.

## Bring-up order

1. **Board-host**: `orchestration/docker/docker-compose.yml` (see its
   README), then apply `orchestration/schema/001_init.sql` and
   `002_seed_topics.sql`.
2. **Sync roles**: `ORCH_BOARD_DSN=... python -m orchestration.roles.sync_roles`
   — prints a fresh MCP bearer token AND a separate webhook secret per role
   (shown once each; they are two DIFFERENT secrets for two different
   trust directions — see the comment on `board.agent.webhook_secret` in
   `001_init.sql`). Copy them into that role's own environment, never into
   git.
3. **MCP server** on the board-host: `orchestration/mcp/server_http.py`
   (needs `ORCH_BOARD_DSN`, `ORCH_MCP_PUBLIC_URL`), behind its own
   Cloudflare Tunnel.
4. **Dispatcher** on the board-host: `orchestration/dispatcher/listen.py`
   (colocated with Postgres, no tunnel needed for its own `LISTEN`
   connection).
5. **Receiver** on each local-role host: copy
   `orchestration/receiver/roles.example.yaml` to `roles.local.yaml`
   (gitignored) with the secrets/tokens from step 2, set `ORCH_MCP_URL` to
   the board-host's tunneled MCP endpoint, run `orchestration/receiver/server.py`
   behind that host's own Cloudflare Tunnel, then use
   `registry.set_webhook_url()` (or a direct `UPDATE board.agent`) to point
   each role's `webhook_url` at that tunnel's `/webhook/<role>` path.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Running the tests

Most of the suite needs a real Postgres reachable at `ORCH_BOARD_DSN` with
the schema applied (a throwaway dev instance is fine — see
`orchestration/docker/README.md`'s dev-vs-real distinction). Tests are
skipped automatically if `ORCH_BOARD_DSN` isn't set.

```bash
export ORCH_BOARD_DSN=postgresql://orchestration:<password>@localhost:5433/orchestration_board
pytest tests/ -v
```

## What's been verified so far vs. what's still unverified

**Verified** (throwaway dev Postgres, torn down after each run — see
`orchestration/docker/README.md`), all as automated tests plus one manual
live-loopback run:
- The full schema (fan-out, durability, id-based reply correlation,
  depth-limited cascades, overseer audit view) — `tests/test_board.py`.
- The real MCP protocol end-to-end via a live stdio subprocess —
  `tests/test_mcp.py`.
- HMAC sign/verify, including the stale-delivery/tampered-body/wrong-secret
  cases — `tests/test_webhook_auth.py`.
- The receiver's single-flight lock, queue-coalescing (N wakeups while busy
  → exactly one follow-up run, not N), and signature/routing HTTP layer —
  `tests/test_receiver.py`.
- The dispatcher's real `LISTEN`/`NOTIFY` loop against real Postgres,
  waking a real HTTP receiver over a real socket with a correctly-signed
  payload referencing the real message id — `tests/test_dispatcher.py`.
- Fail-fast-on-permanent-failure delivery behavior (a 401 doesn't burn the
  full retry/backoff schedule) — `tests/test_deliver.py`.

**Not yet verified — you'll need a real board-host, a real tunnel, and
real `claude`/`codex` binaries to close these out:**
- `claude -p` / `codex exec` exact current flag names (the runners are
  written against what was documented at the time, flagged inline in
  `orchestration/receiver/runners/*.py`).
- The `mcp` SDK's `TokenVerifier`/`AuthSettings` wiring in
  `orchestration/mcp/server_http.py` — written against the documented
  protocol, exercised only via stdio so far.
- Cross-network behavior through an actual Cloudflare Tunnel (everything
  above was proven on `localhost`).
- A cloud-hosted role (e.g. Anthropic Managed Agents) — deliberately out
  of scope for a first deployment; it solves wake-up reachability but not
  board reachability unless you can attach a remote MCP server to it.

Treat this list as the concrete next-step checklist once a board-host
actually exists. `orchestration/ops/scripts/verify_monitor_e2e.sh` is a
manual (not automated) end-to-end checklist for the simplest role once
real infrastructure is up.
