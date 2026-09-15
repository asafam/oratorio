"""Which Postgres LISTEN channels the dispatcher needs, derived from the
current agent/topic registry -- never a fixed firehose channel."""
from __future__ import annotations

from psycopg import Connection

TOPOLOGY_CHANNEL = "bm_topology"
BROADCAST_CHANNEL = "bm_broadcast"


def active_channels(conn: Connection) -> set[str]:
    channels = {TOPOLOGY_CHANNEL, BROADCAST_CHANNEL}
    channels |= {
        r[0] for r in conn.execute("SELECT channel FROM board.topic").fetchall()
    }
    channels |= {
        "bm_direct_" + r[0]
        for r in conn.execute(
            "SELECT substr(md5(agent_id), 1, 16) FROM board.agent WHERE active"
        ).fetchall()
    }
    return channels
