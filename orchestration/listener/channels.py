"""Which Postgres LISTEN channels one agent's listener needs, derived from
the current registry -- never a fixed firehose channel. The names come
from the database's own functions (schema/004_workspaces.sql), the same
ones the fan-out trigger uses, so the two can't drift apart."""
from __future__ import annotations

from psycopg import Connection

TOPOLOGY_CHANNEL = "bm_topology"


def agent_channels(conn: Connection, agent_id: str) -> set[str]:
    """Its own direct channel, its workspace's broadcast channel, the
    topics it is subscribed to (within its workspace), and the topology
    hint (so it re-derives this set when subscriptions change)."""
    channels = {TOPOLOGY_CHANNEL}
    channels |= set(conn.execute(
        "SELECT board.direct_channel(agent_id), board.broadcast_channel(workspace) "
        "FROM board.agent WHERE agent_id = %s",
        (agent_id,),
    ).fetchone())
    channels |= {
        r[0]
        for r in conn.execute(
            "SELECT board.topic_channel(a.workspace, s.topic) FROM board.subscription s "
            "JOIN board.agent a ON a.agent_id = s.agent_id WHERE s.agent_id = %s",
            (agent_id,),
        ).fetchall()
    }
    return channels
