-- Sync log: one row per successful Fivetran sync_end webhook received.
--
-- Written by the webhook receiver BEFORE the schema hash gate, so every
-- successful sync is recorded even when the schema is unchanged and the
-- detection pipeline exits cheap.  This makes the table the authoritative
-- source for freshness queries — schema_snapshots only captures the subset
-- of syncs that produced a schema change.
--
-- synced_at: Fivetran-reported completion time (payload["created"]).
-- received_at: wall-clock time the webhook was received; used for
--   receiver-latency monitoring.

CREATE TABLE IF NOT EXISTS `agent_state.sync_log` (
  log_id        STRING    NOT NULL,   -- UUID, dedup key
  connection_id STRING    NOT NULL,
  sync_id       STRING,               -- Fivetran sync_id, nullable (not in all payloads)
  synced_at     TIMESTAMP NOT NULL,   -- Fivetran-reported sync completion time
  received_at   TIMESTAMP NOT NULL    -- wall-clock time webhook arrived at receiver
)
PARTITION BY DATE(synced_at)
CLUSTER BY connection_id;
