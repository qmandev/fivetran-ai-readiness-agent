-- Sync failure log: one row per Fivetran sync failure event.
--
-- Populated via Fivetran's external-logging API (see scripts/setup_external_logging.sh).
-- Each row captures one failed sync event. The agent's diagnose_sync_failures() tool
-- queries this table to identify error patterns and call Gemini for root-cause analysis.
--
-- error_code:    Fivetran error code (e.g. "SCHEMA_CHANGE_REQUIRED", "CONNECTION_FAILED").
-- error_message: human-readable message from the Fivetran failure event.
-- table_name:    affected table if the failure was table-specific; NULL for connector-level errors.
--
-- The tool degrades gracefully when this table is empty (external-logging not yet configured)
-- by returning {"status": "no_failures"} without calling Gemini.

CREATE TABLE IF NOT EXISTS `agent_state.sync_failure_log` (
  log_id        STRING    NOT NULL,   -- UUID, dedup key
  connection_id STRING    NOT NULL,
  sync_id       STRING,               -- Fivetran sync_id, nullable
  error_code    STRING,               -- Fivetran error code
  error_message STRING,               -- human-readable failure detail
  failed_at     TIMESTAMP NOT NULL,
  table_name    STRING                -- affected table, NULL for connector-level errors
)
PARTITION BY DATE(failed_at)
CLUSTER BY connection_id;
