-- Drift scenario: promote `orders.amount` from NUMERIC(10,2) → TEXT.
-- Per the connector docs' type hierarchy this should land as STRING in BQ
-- (current observed type is BIGNUMERIC). The UPDATE is mandatory — pure
-- DDL doesn't propagate. The trailing SELECT now() yields T_src.
--
-- Hypothesis being tested:
--   • Fivetran preserves the column NAME (`amount` → `amount`)
--   • Fivetran rewrites the BQ column with type STRING
--   • Column ordinal MAY change (docs warn type promotion can reorder)
-- The harness's detect.sql asserts the type changed; reorder is observed
-- after the run by re-querying INFORMATION_SCHEMA.

ALTER TABLE orders ALTER COLUMN amount TYPE TEXT USING amount::text;
UPDATE orders SET amount = amount, updated_at = now() WHERE order_id = 1;
SELECT now() AS source_change_complete;
