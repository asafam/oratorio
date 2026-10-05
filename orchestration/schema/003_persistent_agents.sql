-- Move from "wake a fresh headless process per message" (webhooks) to
-- persistent agents: each agent is one long-lived session with a small
-- listener beside it that LISTENs on the board and hands messages in.
--
--   * Per-message "wait or not": `expires_at` (drop if not handled by
--     then) and `reply_by` (sender's deadline for an answer). Neither set
--     means the message waits in the inbox as long as it takes.
--   * `thread_id` groups related messages, so a listener can tell a new
--     piece of work from a continuation (its hint for suggesting a
--     /clear or /compact before handing the message in).
--   * Delivery state is now two steps: `delivered_at` (the listener handed
--     the message to the session) and `acked_at` (the agent finished with
--     it). A message is done only when acked -- reading it, or a session
--     being cleared mid-task, never consumes it.
--   * Webhook/wake-budget columns go away with the webhook path.

-- ---------------------------------------------------------------------
-- Messages
-- ---------------------------------------------------------------------
ALTER TABLE board.message
    ADD COLUMN expires_at             TIMESTAMPTZ,
    ADD COLUMN reply_by               TIMESTAMPTZ,
    ADD COLUMN reply_timeout_noted_at TIMESTAMPTZ,  -- set once, when the sender's listener has
                                                    -- told it "no reply arrived by reply_by";
                                                    -- alongside `status`, the only column ever
                                                    -- UPDATEd on this otherwise append-only log
    ADD COLUMN thread_id              TEXT,
    ADD CONSTRAINT reply_by_requires_expects_reply CHECK (reply_by IS NULL OR expects_reply);

CREATE INDEX idx_message_thread ON board.message(thread_id, id) WHERE thread_id IS NOT NULL;
CREATE INDEX idx_message_awaiting_reply ON board.message(sender_agent_id, reply_by)
    WHERE reply_by IS NOT NULL AND reply_timeout_noted_at IS NULL;

-- overseer_feed selects m.*, which Postgres expands when the view is
-- created -- recreate it so the new columns show up in the audit read.
DROP VIEW board.overseer_feed;
CREATE VIEW board.overseer_feed AS
SELECT m.*, a.agent_id AS sender_role
FROM board.message m
JOIN board.agent a ON a.agent_id = m.sender_agent_id
ORDER BY m.id;

-- ---------------------------------------------------------------------
-- Delivery: read_at -> delivered_at + acked_at
-- ---------------------------------------------------------------------
ALTER TABLE board.message_delivery ADD COLUMN acked_at TIMESTAMPTZ;

-- Anything already read under the old model counts as done.
UPDATE board.message_delivery SET acked_at = read_at WHERE read_at IS NOT NULL;

-- delivered_at used to mean "row created" (DEFAULT now()). It now means
-- "handed to the agent's session", so it starts out NULL.
ALTER TABLE board.message_delivery
    ALTER COLUMN delivered_at DROP NOT NULL,
    ALTER COLUMN delivered_at DROP DEFAULT;
UPDATE board.message_delivery SET delivered_at = NULL WHERE acked_at IS NULL;

DROP INDEX board.idx_delivery_unread;
ALTER TABLE board.message_delivery DROP COLUMN read_at;
CREATE INDEX idx_delivery_pending ON board.message_delivery(agent_id, message_id) WHERE acked_at IS NULL;

-- The ONE definition of "this agent still has work waiting". Every caller
-- that asks that question reads this view rather than re-deriving it.
CREATE VIEW board.pending_delivery AS
SELECT d.message_id, d.agent_id, d.delivered_at
FROM board.message_delivery d
JOIN board.message m ON m.id = d.message_id
WHERE d.acked_at IS NULL
  AND (m.expires_at IS NULL OR m.expires_at > now());

-- ---------------------------------------------------------------------
-- Agents: drop the webhook path, add presence
-- ---------------------------------------------------------------------
ALTER TABLE board.agent
    DROP COLUMN webhook_url,
    DROP COLUMN webhook_secret,
    DROP COLUMN wakes_this_hour,
    DROP COLUMN wake_budget,
    DROP COLUMN wake_window_started_at,
    ADD COLUMN runner       TEXT,         -- what the agent said it is when it registered (claude, codex, ...)
    ADD COLUMN last_seen_at TIMESTAMPTZ;  -- heartbeat; "online" is derived from this, never stored

-- A listener only LISTENs on the topic channels its own agent is
-- subscribed to, so it needs the same topology hint when that set changes.
CREATE TRIGGER trg_subscription_topology
AFTER INSERT OR DELETE ON board.subscription
FOR EACH ROW EXECUTE FUNCTION board.notify_topology_changed();
