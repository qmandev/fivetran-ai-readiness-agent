-- Detection predicate: returns 1 when the new column has propagated to BQ.
-- The harness polls this query and considers the scenario "detected" when
-- the returned integer is > 0.
SELECT COUNT(*)
FROM `api-project-910787152095.public.INFORMATION_SCHEMA.COLUMNS`
WHERE table_name = 'customers' AND column_name = 'email_verified';
