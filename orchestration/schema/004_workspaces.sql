-- Workspaces: several independent teams on one board.
--
-- A workspace is a name plus the agents in it. `manager` in workspace
-- `thesis` and `manager` in workspace `ablations` are two different
-- agents that never see each other's messages.
--
--   * Identity does not change shape: `board.agent.agent_id` stays the
--     one opaque key everything else references (deliveries, tokens,
--     LISTEN channels). An agent additionally has a (workspace, name)
--     pair, unique together -- `name` is what agents and people type.
--   * Separation is enforced HERE, in the database, not only in the
--     Python above it: a message can only be addressed to, or be a reply
--     within, the sender's own workspace (scope_message), and topic /
--     broadcast fan-out only reaches the sender's workspace
--     (fanout_message). No caller -- MCP tool, test, hand-written SQL --
--     can cross.
--   * Topic names stay global vocabulary (`results` means the same thing
--     everywhere); it is the fan-out that is scoped. LISTEN channels for
--     topics and broadcasts are per workspace, so a post in one workspace
--     does not even wake listeners in another.

CREATE TABLE board.workspace (
    name       TEXT PRIMARY KEY CHECK (name ~ '^[a-z0-9][a-z0-9_-]{0,40}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Everything that existed before workspaces lives in this one.
INSERT INTO board.workspace (name) VALUES ('default');

-- ---------------------------------------------------------------------
-- Agents
-- ---------------------------------------------------------------------
ALTER TABLE board.agent
    ADD COLUMN workspace TEXT REFERENCES board.workspace(name),
    ADD COLUMN name      TEXT;
UPDATE board.agent SET workspace = 'default', name = agent_id;
ALTER TABLE board.agent
    ALTER COLUMN workspace SET NOT NULL,
    ALTER COLUMN name SET NOT NULL,
    ADD CONSTRAINT agent_name_unique_in_workspace UNIQUE (workspace, name);

-- ---------------------------------------------------------------------
-- Messages carry their workspace (always the sender's -- set by the
-- trigger below, never by the caller).
-- ---------------------------------------------------------------------
ALTER TABLE board.message ADD COLUMN workspace TEXT REFERENCES board.workspace(name);
UPDATE board.message m SET workspace = a.workspace
  FROM board.agent a WHERE a.agent_id = m.sender_agent_id;
ALTER TABLE board.message ALTER COLUMN workspace SET NOT NULL;
CREATE INDEX idx_message_workspace ON board.message(workspace, id);

CREATE FUNCTION board.scope_message() RETURNS trigger AS $$
BEGIN
    SELECT workspace INTO NEW.workspace FROM board.agent WHERE agent_id = NEW.sender_agent_id;

    IF NEW.recipient_agent_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM board.agent
         WHERE agent_id = NEW.recipient_agent_id AND workspace = NEW.workspace
    ) THEN
        RAISE EXCEPTION 'recipient is not in the sender''s workspace (%)', NEW.workspace
              USING ERRCODE = 'check_violation';
    END IF;

    IF NEW.in_reply_to IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM board.message
         WHERE id = NEW.in_reply_to AND workspace = NEW.workspace
    ) THEN
        RAISE EXCEPTION 'in_reply_to is not a message in the sender''s workspace (%)', NEW.workspace
              USING ERRCODE = 'check_violation';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_scope_message
BEFORE INSERT ON board.message
FOR EACH ROW EXECUTE FUNCTION board.scope_message();

-- ---------------------------------------------------------------------
-- LISTEN channel names, defined once so the fan-out trigger and the
-- listeners (orchestration/listener/channels.py) can never disagree.
-- ---------------------------------------------------------------------
CREATE FUNCTION board.topic_channel(ws TEXT, topic TEXT) RETURNS TEXT AS $$
    SELECT 'bm_t_' || substr(md5(ws || '/' || topic), 1, 16)
$$ LANGUAGE sql IMMUTABLE;

CREATE FUNCTION board.broadcast_channel(ws TEXT) RETURNS TEXT AS $$
    SELECT 'bm_b_' || substr(md5(ws), 1, 16)
$$ LANGUAGE sql IMMUTABLE;

CREATE FUNCTION board.direct_channel(agent_id TEXT) RETURNS TEXT AS $$
    SELECT 'bm_direct_' || substr(md5(agent_id), 1, 16)
$$ LANGUAGE sql IMMUTABLE;

-- The per-topic channel column from 001 is replaced by topic_channel().
ALTER TABLE board.topic DROP COLUMN channel;

-- ---------------------------------------------------------------------
-- Fan-out, scoped to the sender's workspace.
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
         WHERE active AND workspace = NEW.workspace AND agent_id <> NEW.sender_agent_id;
        chan := board.broadcast_channel(NEW.workspace);
    ELSIF NEW.topic IS NOT NULL THEN
        INSERT INTO board.message_delivery (message_id, agent_id)
        SELECT NEW.id, s.agent_id FROM board.subscription s
         JOIN board.agent a ON a.agent_id = s.agent_id AND a.active
         WHERE s.topic = NEW.topic AND a.workspace = NEW.workspace
           AND s.agent_id <> NEW.sender_agent_id;
        chan := board.topic_channel(NEW.workspace, NEW.topic);
    ELSE
        INSERT INTO board.message_delivery (message_id, agent_id)
        VALUES (NEW.id, NEW.recipient_agent_id);
        chan := board.direct_channel(NEW.recipient_agent_id);
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

-- ---------------------------------------------------------------------
-- Audit view: now carries the workspace (via m.*) and readable names.
-- An auditor only ever reads its own workspace through this
-- (registry.read_all_messages filters on it).
-- ---------------------------------------------------------------------
DROP VIEW board.overseer_feed;
CREATE VIEW board.overseer_feed AS
SELECT m.*, s.name AS sender, r.name AS recipient
FROM board.message m
JOIN board.agent s ON s.agent_id = m.sender_agent_id
LEFT JOIN board.agent r ON r.agent_id = m.recipient_agent_id
ORDER BY m.id;
