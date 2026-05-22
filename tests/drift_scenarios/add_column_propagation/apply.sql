-- Drift scenario: add a column at the source.
-- Pure DDL won't propagate via Fivetran (per connector docs) — the UPDATE is
-- mandatory to trigger an incremental row pickup. The trailing SELECT now()
-- yields the precise T_src reference timestamp returned to the harness.
ALTER TABLE customers ADD COLUMN email_verified BOOLEAN DEFAULT FALSE;
UPDATE customers SET email_verified=TRUE, updated_at=now() WHERE customer_id=1;
SELECT now() AS source_change_complete;
