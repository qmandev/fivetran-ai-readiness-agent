-- Drift events: classified changes + remediation lifecycle.
-- This is the audit trail that keeps the human in control.

CREATE TABLE IF NOT EXISTS `agent_state.drift_events` (
  drift_id            STRING NOT NULL,       -- UUID
  connection_id       STRING NOT NULL,
  detected_at         TIMESTAMP NOT NULL,
  from_snapshot_id    STRING NOT NULL,
  to_snapshot_id      STRING NOT NULL,
  table_schema        STRING NOT NULL,
  table_name          STRING NOT NULL,
  change_type         STRING NOT NULL,       -- RENAME|TYPE_PROMOTION|REORDER|NEW_FIELD|DEPRECATION
  column_before       JSON,
  column_after        JSON,
  classification_conf FLOAT64,               -- Gemini confidence 0..1
  gemini_rationale    STRING,
  remediation_sql     STRING,                -- generated VIEW shim
  transformation_id   STRING,                -- Fivetran transformation created on apply
  remediation_status  STRING NOT NULL,       -- PROPOSED|APPROVED|APPLIED|REJECTED|VERIFIED
  approved_by         STRING,
  updated_at          TIMESTAMP NOT NULL
)
PARTITION BY DATE(detected_at)
CLUSTER BY connection_id, remediation_status;
