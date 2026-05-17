-- Column-level snapshot. ordinal_position is load-bearing: Fivetran
-- reorders columns on automatic type promotion (Core Concepts A5).

CREATE TABLE IF NOT EXISTS `agent_state.column_snapshots` (
  snapshot_id      STRING NOT NULL,
  connection_id    STRING NOT NULL,
  table_schema     STRING NOT NULL,
  table_name       STRING NOT NULL,
  column_name      STRING NOT NULL,
  data_type        STRING NOT NULL,
  ordinal_position INT64 NOT NULL,
  is_nullable      BOOL,
  captured_at      TIMESTAMP NOT NULL       -- denormalized for partition/cluster
)
PARTITION BY DATE(captured_at)
CLUSTER BY connection_id, table_name;
