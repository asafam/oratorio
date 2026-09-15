"""Agent/role/topic registry operations, plus the overseer's full-visibility
audit read. Kept separate from messages.py since these are registry reads/
writes, not the message-delivery hot path.
"""
from __future__ import annotations

from typing import Any

from psycopg import Connection
from psycopg.rows import dict_row


class AuthorizationError(Exception):
    pass


def list_agents(conn: Connection, *, active_only: bool = True) -> list[dict[str, Any]]:
    where = "WHERE active" if active_only else ""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"SELECT agent_id, brief, topics, peers FROM board.agent {where} ORDER BY agent_id"
        )
        return cur.fetchall()


def get_role(conn: Connection, agent_id: str) -> dict[str, Any] | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT agent_id, role_doc_path, role_version, brief, peers, topics "
            "FROM board.agent WHERE agent_id = %s",
            (agent_id,),
        )
        return cur.fetchone()


def list_topics(conn: Connection) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT topic, description FROM board.topic ORDER BY topic")
        return cur.fetchall()


def subscribe(conn: Connection, agent_id: str, topic: str, *, backfill: bool = False) -> None:
    conn.execute(
        "INSERT INTO board.subscription (agent_id, topic) VALUES (%s, %s) "
        "ON CONFLICT DO NOTHING",
        (agent_id, topic),
    )
    if backfill:
        # Explicit opt-in only -- normal pub/sub semantics otherwise mean a
        # late subscriber gets no history, which is the correct default.
        conn.execute(
            """
            INSERT INTO board.message_delivery (message_id, agent_id)
            SELECT m.id, %s FROM board.message m
            WHERE m.topic = %s AND m.sender_agent_id <> %s
            ON CONFLICT DO NOTHING
            """,
            (agent_id, topic, agent_id),
        )


def try_consume_wake_budget(conn: Connection, agent_id: str) -> dict[str, Any] | None:
    """Atomically check-and-increment this agent's rolling wake budget --
    combining the read (active? under budget?) and the write (increment)
    into one UPDATE avoids a check-then-act race between two dispatcher
    wake attempts landing concurrently. Lazily resets the counter if the
    rolling 1h window has elapsed, so no external cron job is needed.
    Returns {webhook_url, webhook_secret} if the caller should proceed with
    delivery, or None if the role is inactive (kill switch) or has hit its
    hourly wake budget (runaway-cascade guard, alongside depth_remaining)."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            UPDATE board.agent
            SET wakes_this_hour = CASE
                    WHEN now() - wake_window_started_at > INTERVAL '1 hour' THEN 1
                    ELSE wakes_this_hour + 1
                END,
                wake_window_started_at = CASE
                    WHEN now() - wake_window_started_at > INTERVAL '1 hour' THEN now()
                    ELSE wake_window_started_at
                END
            WHERE agent_id = %s AND active
              AND (
                    now() - wake_window_started_at > INTERVAL '1 hour'
                    OR wakes_this_hour < wake_budget
                  )
            RETURNING webhook_url, webhook_secret
            """,
            (agent_id,),
        )
        return cur.fetchone()


def set_webhook_url(conn: Connection, agent_id: str, webhook_url: str) -> None:
    """Set/update where the dispatcher should POST a wake-up for this role.
    Separate from upsert_agent (called at role-sync time, before the
    receiver's tunnel URL is necessarily known yet) -- an ops step run once
    the role's receiver + tunnel are actually up."""
    conn.execute(
        "UPDATE board.agent SET webhook_url = %s, updated_at = now() WHERE agent_id = %s",
        (webhook_url, agent_id),
    )


def unsubscribe(conn: Connection, agent_id: str, topic: str) -> None:
    conn.execute(
        "DELETE FROM board.subscription WHERE agent_id = %s AND topic = %s",
        (agent_id, topic),
    )


def read_all_messages(
    conn: Connection, agent_id: str, *, since_id: int = 0, limit: int = 200
) -> list[dict[str, Any]]:
    """Auditor-only full history read, bypassing message_delivery entirely.
    Gated on board.agent.is_auditor -- the one place authorization is
    actually enforced in this design (everywhere else deliberately trusts
    every registered agent, since all of them belong to the same operator
    and there's no adversarial case to defend against), because this tool
    bypasses the per-agent delivery model and misuse of it is a real
    footgun even in a fully trusted, single-owner setting.
    """
    row = conn.execute(
        "SELECT is_auditor FROM board.agent WHERE agent_id = %s", (agent_id,)
    ).fetchone()
    if row is None or not row[0]:
        raise AuthorizationError(f"{agent_id!r} is not an auditor; read_all_messages refused")

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT * FROM board.overseer_feed WHERE id > %s ORDER BY id LIMIT %s",
            (since_id, limit),
        )
        return cur.fetchall()


def upsert_agent(
    conn: Connection,
    *,
    agent_id: str,
    role_doc_path: str,
    role_version: str,
    brief: str,
    peers: list[str],
    topics: list[str],
    auth_token_hash: str,
    is_auditor: bool = False,
    webhook_url: str | None = None,
    webhook_secret: str | None = None,
) -> None:
    """Used by orchestration/roles/sync_roles.py to load a role's markdown
    file into the registry. Git (the role file) is authoritative; this row
    is a derived cache -- role_version (a git blob sha) lets a caller detect
    a stale DB copy."""
    # webhook_url/webhook_secret are intentionally NOT in the UPDATE SET
    # list below, same as auth_token_hash: sync_roles.py is responsible for
    # fetching-or-generating the right value BEFORE calling this function
    # (see its need_token/need_secret logic), so re-syncing role metadata
    # (brief, peers, topics) on every run never silently clobbers a secret.
    conn.execute(
        """
        INSERT INTO board.agent
            (agent_id, role_doc_path, role_version, brief, peers, topics,
             auth_token_hash, is_auditor, webhook_url, webhook_secret)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (agent_id) DO UPDATE SET
            role_doc_path = EXCLUDED.role_doc_path,
            role_version = EXCLUDED.role_version,
            brief = EXCLUDED.brief,
            peers = EXCLUDED.peers,
            topics = EXCLUDED.topics,
            is_auditor = EXCLUDED.is_auditor,
            updated_at = now()
        """,
        (
            agent_id,
            role_doc_path,
            role_version,
            brief,
            peers,
            topics,
            auth_token_hash,
            is_auditor,
            webhook_url,
            webhook_secret,
        ),
    )
    # Reconcile subscriptions to exactly match `topics` -- add what's
    # missing, remove what's no longer listed. Git (the role file) is
    # authoritative, so an edit that drops a topic must actually revoke
    # that subscription, not just leave a stale row from a previous sync.
    conn.execute(
        "DELETE FROM board.subscription WHERE agent_id = %s AND NOT (topic = ANY(%s))",
        (agent_id, topics),
    )
    for topic in topics:
        conn.execute(
            "INSERT INTO board.subscription (agent_id, topic) VALUES (%s, %s) "
            "ON CONFLICT DO NOTHING",
            (agent_id, topic),
        )
