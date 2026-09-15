-- Orchestration board schema.
-- Run against a dedicated database (e.g. `orchestration_board`), never a
-- database shared with another, unrelated service -- this schema should
-- not be coupled to another service's lifecycle/uptime.
--
-- Design notes (see orchestration/README.md for the full rationale):
--   * `board.message_delivery` is the durable queue. `pg_notify()` in the
--     fan-out trigger below is a latency optimization only -- a dropped
--     LISTEN connection never loses a message, because the delivery row
--     already exists before NOTIFY fires (same transaction).
--   * Correlation (request/reply) always uses `board.message.id`, the
--     harness-minted bigserial -- never sender/recipient identity. Keying
--     pending-request state off a peer instead of the id the system
--     already minted is a classic source of bugs: a second concurrent ask
--     to the same peer gets mistaken for a duplicate, or an out-of-order
--     reply closes the wrong request.
--   * `depth_remaining` is a hop budget that bounds agent-to-agent message
--     cascades (A wakes B wakes A ...).

CREATE SCHEMA IF NOT EXISTS board;

-- ---------------------------------------------------------------------
-- Agent / role registry
-- ---------------------------------------------------------------------
CREATE TABLE board.agent (
    agent_id        TEXT PRIMARY KEY,
    role_doc_path   TEXT NOT NULL,
    role_version    TEXT NOT NULL,
    brief           TEXT NOT NULL,
    peers           TEXT[] NOT NULL DEFAULT '{}',
    topics          TEXT[] NOT NULL DEFAULT '{}',
    webhook_url     TEXT,
    auth_token_hash TEXT NOT NULL,     -- sha256; used for MCP auth (board verifies a caller)
    webhook_secret  TEXT,              -- PLAINTEXT, unlike auth_token_hash: the dispatcher
                                        -- (on the board-host, same trust boundary as this DB)
                                        -- must read the raw secret directly to HMAC-sign
                                        -- outbound webhook deliveries -- a hash can verify but
                                        -- can't originate a signature. Deliberately a SEPARATE
                                        -- secret from auth_token_hash: one flows board->agent
                                        -- (webhook), the other agent->board (MCP).
    is_auditor      BOOLEAN NOT NULL DEFAULT FALSE,
    wakes_this_hour INTEGER NOT NULL DEFAULT 0,
    wake_budget     INTEGER NOT NULL DEFAULT 30,
    wake_window_started_at TIMESTAMPTZ NOT NULL DEFAULT now(),  -- rolling 1h window for
                                                                 -- wakes_this_hour, reset lazily
                                                                 -- on check (see
                                                                 -- registry.try_consume_wake_budget)
                                                                 -- rather than needing an external cron
    active          BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- Topics -- explicit registry so LISTEN channel derivation is centralized.
-- Channel names are derived (md5-hashed), never authored, so they can
-- never collide or exceed Postgres's 63-byte identifier cap regardless of
-- topic name length/case.
-- ---------------------------------------------------------------------
CREATE TABLE board.topic (
    topic       TEXT PRIMARY KEY CHECK (topic ~ '^[a-z0-9][a-z0-9_-]{0,62}$'),
    description TEXT,
    channel     TEXT GENERATED ALWAYS AS ('bm_' || substr(md5(topic), 1, 16)) STORED
);

CREATE TABLE board.subscription (
    agent_id      TEXT NOT NULL REFERENCES board.agent(agent_id) ON DELETE CASCADE,
    topic         TEXT NOT NULL REFERENCES board.topic(topic) ON DELETE CASCADE,
    subscribed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (agent_id, topic)
);

-- ---------------------------------------------------------------------
-- Messages -- append-only log. Never UPDATEd except `status`, never deleted.
-- ---------------------------------------------------------------------
CREATE TABLE board.message (
    id                  BIGSERIAL PRIMARY KEY,
    sender_agent_id     TEXT NOT NULL REFERENCES board.agent(agent_id),
    recipient_agent_id  TEXT REFERENCES board.agent(agent_id),
    topic               TEXT REFERENCES board.topic(topic),
    is_broadcast        BOOLEAN NOT NULL DEFAULT FALSE,
    msg_type            TEXT NOT NULL DEFAULT 'DOMAIN'
                        CHECK (msg_type IN ('DOMAIN','ADMIN','EVENT','REPLY','HEARTBEAT','PLAN','ALERT')),
    content             TEXT NOT NULL,
    in_reply_to         BIGINT REFERENCES board.message(id),
    depth_remaining     SMALLINT NOT NULL DEFAULT 8,
    expects_reply       BOOLEAN NOT NULL DEFAULT FALSE,
    status              TEXT NOT NULL DEFAULT 'sent' CHECK (status IN ('sent','superseded','retracted')),
    metadata            JSONB NOT NULL DEFAULT '{}',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT reply_requires_parent CHECK (msg_type <> 'REPLY' OR in_reply_to IS NOT NULL),
    CONSTRAINT exactly_one_route CHECK (
        (is_broadcast AND recipient_agent_id IS NULL AND topic IS NULL) OR
        (NOT is_broadcast AND recipient_agent_id IS NOT NULL AND topic IS NULL) OR
        (NOT is_broadcast AND recipient_agent_id IS NULL AND topic IS NOT NULL)
    )
);

CREATE INDEX idx_message_topic       ON board.message(topic, id);
CREATE INDEX idx_message_recipient   ON board.message(recipient_agent_id, id);
CREATE INDEX idx_message_in_reply_to ON board.message(in_reply_to);

-- ---------------------------------------------------------------------
-- Per-agent delivery/read state. THE durable queue. One row per
-- (message, agent) for every intended recipient, materialized in the same
-- transaction as the message insert (see trigger below) -- a reader doing
-- `WHERE agent_id = X AND read_at IS NULL` never depends on whether it was
-- listening at the moment the message was posted.
-- ---------------------------------------------------------------------
CREATE TABLE board.message_delivery (
    message_id   BIGINT NOT NULL REFERENCES board.message(id) ON DELETE CASCADE,
    agent_id     TEXT NOT NULL REFERENCES board.agent(agent_id) ON DELETE CASCADE,
    delivered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    read_at      TIMESTAMPTZ,
    PRIMARY KEY (message_id, agent_id)
);

CREATE INDEX idx_delivery_unread ON board.message_delivery(agent_id, message_id) WHERE read_at IS NULL;

-- ---------------------------------------------------------------------
-- Overseer visibility: full audit trail, NOT a subscription. Auditing
-- isn't "consuming" -- this view must never generate message_delivery rows.
-- ---------------------------------------------------------------------
CREATE VIEW board.overseer_feed AS
SELECT m.*, a.agent_id AS sender_role
FROM board.message m
JOIN board.agent a ON a.agent_id = m.sender_agent_id
ORDER BY m.id;

-- ---------------------------------------------------------------------
-- Fan-out trigger: explodes message_delivery rows + fires pg_notify().
-- depth_remaining <= 0 short-circuits delivery entirely (hop-budget guard
-- against runaway agent-to-agent wake cascades).
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION board.fanout_message() RETURNS trigger AS $$
DECLARE
    chan TEXT;
BEGIN
    IF NEW.depth_remaining <= 0 THEN
        RETURN NEW;
    END IF;

    IF NEW.is_broadcast THEN
        INSERT INTO board.message_delivery (message_id, agent_id)
        SELECT NEW.id, agent_id FROM board.agent
         WHERE active AND agent_id <> NEW.sender_agent_id;
        chan := 'bm_broadcast';
    ELSIF NEW.topic IS NOT NULL THEN
        INSERT INTO board.message_delivery (message_id, agent_id)
        SELECT NEW.id, s.agent_id FROM board.subscription s
         JOIN board.agent a ON a.agent_id = s.agent_id AND a.active
         WHERE s.topic = NEW.topic AND s.agent_id <> NEW.sender_agent_id;
        SELECT channel INTO chan FROM board.topic WHERE topic = NEW.topic;
    ELSE
        INSERT INTO board.message_delivery (message_id, agent_id)
        VALUES (NEW.id, NEW.recipient_agent_id);
        chan := 'bm_direct_' || substr(md5(NEW.recipient_agent_id), 1, 16);
    END IF;

    -- Payload is metadata only (Postgres caps NOTIFY payloads at 8000
    -- bytes; message.content has no length bound). Listeners always
    -- re-SELECT for the actual content.
    PERFORM pg_notify(chan, json_build_object(
        'message_id', NEW.id,
        'topic', NEW.topic,
        'msg_type', NEW.msg_type
    )::text);

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_fanout_message
AFTER INSERT ON board.message
FOR EACH ROW EXECUTE FUNCTION board.fanout_message();

-- ---------------------------------------------------------------------
-- Topology-change notification: the dispatcher needs to know when a new
-- agent/topic appears (or an agent is deactivated) so it can add/refresh
-- the LISTEN channels it's watching, without polling the registry on a
-- fixed timer. This is a hint, same as message NOTIFYs -- the dispatcher
-- also re-derives its full channel set from board.agent/board.topic
-- directly on every reconnect, so a missed 'bm_topology' NOTIFY only
-- delays picking up a new channel, never silently drops delivery of a
-- message on an existing one.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION board.notify_topology_changed() RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify('bm_topology', 'changed');
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_agent_topology
AFTER INSERT OR UPDATE OF active OR DELETE ON board.agent
FOR EACH ROW EXECUTE FUNCTION board.notify_topology_changed();

CREATE TRIGGER trg_topic_topology
AFTER INSERT OR DELETE ON board.topic
FOR EACH ROW EXECUTE FUNCTION board.notify_topology_changed();
