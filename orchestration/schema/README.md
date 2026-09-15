# Schema migrations

Forward-only, numbered SQL files (`NNN_description.sql`), applied in order.
No migration framework — at this scale (a handful of tables, one small
team) a numbered-file convention plus `psql` is simpler to operate than
Alembic/etc.

Apply with:

```bash
psql "$ORCH_BOARD_DSN" -f orchestration/schema/001_init.sql
psql "$ORCH_BOARD_DSN" -f orchestration/schema/002_seed_topics.sql
```

`ORCH_BOARD_DSN` points at the `orchestration_board` database on the
board-host (see `orchestration/docker/README.md` for how that database is
brought up) — never a database shared with another, unrelated service.

When adding a migration: create `NNN_description.sql` with the next number,
never edit a previously-applied file in place (append a new migration
instead, even to fix a mistake — the numbered files are the audit trail of
what ran, in order, against real data).
