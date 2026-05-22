-- Detection predicate: returns 1 when `orders.amount` in BQ has a type
-- OTHER than BIGNUMERIC (its current landed type). Most likely STRING
-- after the source promotion to TEXT.
SELECT COUNT(*)
FROM `api-project-910787152095.public.INFORMATION_SCHEMA.COLUMNS`
WHERE table_name = 'orders'
  AND column_name = 'amount'
  AND data_type != 'BIGNUMERIC';
