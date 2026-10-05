"""Which Postgres LISTEN channels one agent's listener needs, derived from
the current registry -- never a fixed firehose channel."""
from __future__ import annotations

from psycopg import Connection

TOPOLOGY_CHANNEL = "bm_topology"
BROADCAST_CHANNEL = "bm_broadcast"


def agent_channels(conn: Connection, agent_id: str) -> set[str]:
    """Its own direct channel, the topics it is subscribed to, broadcasts,
    and the topology hint (so it re-derives this set when subscriptions
    change)."""
    channels = {TOPOLOGY_CHANNEL, BROADCAST_CHANNEL}
    channels.add(
        conn.execute("SELECT 'bm_direct_' || substr(md5(%s), 1, 16)", (agent_id,)).fetchone()[0]
    )
    channels |= {
        r[0]
        for r in conn.execute(
            "SELECT t.channel FROM board.subscription s JOIN board.topic t ON t.topic = s.topic "
            "WHERE s.agent_id = %s",
            (agent_id,),
        ).fetchall()
    }
    return channels
