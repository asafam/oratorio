# Orchestration board — Postgres

Runs the board's Postgres. **Deploy this on the board-host — a small,
independent, always-on machine you control — never on a shared/HPC-style
host you don't fully control.** A shared host typically gets rebooted for
maintenance outside your control, and may not even be directly reachable
without a proxy/jump host; none of that is acceptable for the one
component every agent's correctness depends on being reachable and
durable. See the project README (`orchestration/README.md`) for the full
reasoning.

This directory can also be used to stand up a throwaway local Postgres for
**development/testing** (Phase 0 of the build: schema + toy-agent tests,
zero network, zero LLM cost) on whatever machine you're coding on — that is
explicitly not the same thing as deploying the real board, and nothing here
should be treated as "the board" until it's running on the board-host.

## Bring-up

```bash
export ORCH_BOARD_PG_PASSWORD=<generate a real secret, don't reuse another service's>
# Optional overrides (defaults shown):
#   ORCH_BOARD_PG_USER=orchestration
#   ORCH_BOARD_PG_PORT=5433
#   ORCH_BOARD_PGDATA=${HOME}/.orchestration-board/pgdata

docker compose -f orchestration/docker/docker-compose.yml up -d
```

Data is bind-mounted under `$HOME` (not an anonymous Docker named volume) —
named volumes live under `/var/lib/docker/volumes`, shared root-owned
storage on any multi-user Docker host; `$HOME` is always this account's own
space. Prefer this convention any time the board (or its dev/test
stand-in) might run on a host shared with other users' containers.

Container and network names (`orchestration-board-postgres`,
`agent-board-net`) are deliberately specific, not generic — on a
Docker host shared with other users/services, generic names risk colliding
with someone else's container.

Apply the schema once the container is up:

```bash
export ORCH_BOARD_DSN="postgresql://orchestration:${ORCH_BOARD_PG_PASSWORD}@localhost:${ORCH_BOARD_PG_PORT:-5433}/orchestration_board"
psql "$ORCH_BOARD_DSN" -f orchestration/schema/001_init.sql
psql "$ORCH_BOARD_DSN" -f orchestration/schema/002_seed_topics.sql
```

## Teardown

```bash
docker compose -f orchestration/docker/docker-compose.yml down
```

(Data under `$ORCH_BOARD_PGDATA` survives a `down` — only `docker compose
down -v` combined with removing that directory manually would actually
discard it, and this compose file has no anonymous volumes to `-v` away.)
