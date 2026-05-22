-- Revert orders.amount back to NUMERIC(10,2). Requires a USING clause to
-- cast the text values; valid only because we kept the same string content
-- ('99.50' etc.) during the promotion.
--
-- NOTE: Fivetran soft-drops at the destination; reverting the source type
-- does NOT automatically rewrite the BQ column type back. To restore BQ
-- to BIGNUMERIC you'd need a reload + sync (option b in the harness header)
-- or accept the destination divergence.
ALTER TABLE orders ALTER COLUMN amount TYPE NUMERIC(10,2) USING amount::numeric(10,2);
UPDATE orders SET updated_at = now() WHERE order_id = 1;
