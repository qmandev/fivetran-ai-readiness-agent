-- Entity map: one row per (entity, connection, table) occurrence detected by
-- detect_entity_overlaps().
--
-- Each detect_entity_overlaps() run writes a batch of rows sharing the same
-- detection_id (UUID).  Rows are append-only — a new detection_id is generated
-- per run.  Downstream queries should filter to the latest detection_id to
-- get the current view of entity overlaps.
--
-- conflicts: JSON array of split-truth conflict strings, e.g.
--   ["email is NOT NULL in conn_a but NULLABLE in conn_b"].
-- join_key_col: the column Gemini suggests as a join key between occurrences.

CREATE TABLE IF NOT EXISTS `agent_state.entity_map` (
  detection_id  STRING    NOT NULL,   -- UUID shared across all rows in one detection run
  entity_name   STRING    NOT NULL,   -- e.g. "Customer", "Order"
  connection_id STRING    NOT NULL,
  table_name    STRING    NOT NULL,   -- schema.table_name in BQ
  join_key_col  STRING,               -- suggested join key column name
  confidence    FLOAT64,              -- 0.0–1.0 Gemini confidence
  conflicts     JSON,                 -- array of split-truth conflict strings
  detected_at   TIMESTAMP NOT NULL
)
PARTITION BY DATE(detected_at)
CLUSTER BY entity_name;
