-- Reset the source side. NOTE: Fivetran soft-drops at the destination
-- (the column persists in BQ with NULL for new rows). To fully remove the
-- BQ column you'd need to either reload the schema config + force a sync,
-- or drop it via the Fivetran MCP `delete_connection_column_config`.
ALTER TABLE customers DROP COLUMN IF EXISTS email_verified;
UPDATE customers SET updated_at=now() WHERE customer_id=1;
