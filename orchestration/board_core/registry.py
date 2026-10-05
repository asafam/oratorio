"""Agent/role/topic registry operations, plus the overseer's full-visibility
audit read. Kept separate from messages.py since these are registry reads/
writes, not the message-delivery hot path.
"""
from __future__ import annotations

from typing import Any

from psycopg import Connection
from psycopg.rows import dict_row


# An agent counts as online if its listener has checked in this recently.
# Derived from last_seen_at on every read -- never a stored status, which
# would go stale the moment a listener dies without saying goodbye.
ONLINE_WINDOW_SECONDS = 120


class AuthorizationError(Exception):
    pass


def list_agents(conn: Connection, *, active_only: bool = True) -> list[dict[str, Any]]:
    where = "WHERE active" if active_only else ""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            SELECT agent_id, brief, topics, peers, runner, last_seen_at,
                   COALESCE(last_seen_at > now() - make_interval(secs => %s), FALSE) AS online
            FROM board.agent {where} ORDER BY agent_id
            """,
            (ONLINE_WINDOW_SECONDS,),
        )
        return cur.fetchall()


def register(
    conn: Connection, agent_id: str, *, runner: str, topics: list[str] | None = None
) -> None:
    """An agent announcing "I'm up" -- called by its listener on start.
    Records what it is and marks it seen; if `topics` is given, its
    subscriptions are set to exactly that list. This does NOT create the
    agent or its token: identity is issued once, out of band (see
    orchestration/roles/sync_roles.py), and `agent_id` here must already
    be resolved from that token."""
    conn.execute(
        "UPDATE board.agent SET runner = %s, last_seen_at = now(), updated_at = now() "
        "WHERE agent_id = %s",
        (runner, agent_id),
    )
    if topics is not None:
        conn.execute(
            "UPDATE board.agent SET topics = %s WHERE agent_id = %s", (topics, agent_id)
        )
        _set_subscriptions(conn, agent_id, topics)


def set_auth_token_hash(conn: Connection, agent_id: str, auth_token_hash: str) -> None:
    """Replace an agent's token (the old one stops working at once)."""
    conn.execute(
        "UPDATE board.agent SET auth_token_hash = %s, updated_at = now() WHERE agent_id = %s",
        (auth_token_hash, agent_id),
    )


def heartbeat(conn: Connection, agent_id: str) -> None:
    conn.execute("UPDATE board.agent SET last_seen_at = now() WHERE agent_id = %s", (agent_id,))


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
) -> None:
    """Used by orchestration/roles/sync_roles.py to load a role's markdown
    file into the registry. Git (the role file) is authoritative; this row
    is a derived cache -- role_version (a git blob sha) lets a caller detect
    a stale DB copy."""
    # auth_token_hash is intentionally NOT in the UPDATE SET list below:
    # sync_roles.py is responsible for fetching-or-generating the right
    # value BEFORE calling this function (see its need_token logic), so
    # re-syncing role metadata (brief, peers, topics) on every run never
    # silently clobbers a token.
    conn.execute(
        """
        INSERT INTO board.agent
            (agent_id, role_doc_path, role_version, brief, peers, topics,
             auth_token_hash, is_auditor)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
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
        ),
    )
    _set_subscriptions(conn, agent_id, topics)


def _set_subscriptions(conn: Connection, agent_id: str, topics: list[str]) -> None:
    # Reconcile subscriptions to exactly match `topics` -- add what's
    # missing, remove what's no longer listed -- so dropping a topic
    # actually revokes that subscription, not just leaves a stale row.
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
