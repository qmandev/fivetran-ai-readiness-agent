-- Snapshot metadata. content_hash enables an O(1) "did anything change?"
-- gate before any column-level diff work.

CREATE TABLE IF NOT EXISTS `agent_state.schema_snapshots` (
  snapshot_id        STRING NOT NULL,        -- UUID
  connection_id      STRING NOT NULL,
  connection_name    STRING,
  destination_schema STRING NOT NULL,
  captured_at        TIMESTAMP NOT NULL,
  trigger_event      STRING NOT NULL,        -- sync_end | manual | scheduled
  sync_id            STRING,                 -- from webhook payload, nullable
  column_count       INT64,
  content_hash       STRING NOT NULL         -- sha256 over sorted column tuples
)
PARTITION BY DATE(captured_at)
CLUSTER BY connection_id;
