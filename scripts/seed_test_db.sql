-- Cloud SQL test DB seed for the Fivetran schema-drift sandbox.
--
-- This file is committed to a PUBLIC repo — it keeps a <FIVETRAN_DB_PASSWORD>
-- PLACEHOLDER. Never hardcode the real password here. Inject it at runtime so
-- the secret only lives in your shell env, never on disk / in git:
--
--   export FIVETRAN_DB_PW='choose-a-strong-unique-password'
--   sed "s/<FIVETRAN_DB_PASSWORD>/$FIVETRAN_DB_PW/" scripts/seed_test_db.sql \
--     | gcloud sql connect ftar-pg --user=postgres
--
-- (Direct-psql alternative: psql ... -v fivetran_pw="$FIVETRAN_DB_PW" and use
--  :'fivetran_pw' below instead of the placeholder.)
--
-- Keep FIVETRAN_DB_PW safe — you paste the same value into the Fivetran
-- PostgreSQL connector setup form (section F). It is the SOURCE DB user,
-- distinct from the Fivetran API key/secret. Use a UNIQUE password (not one
-- reused elsewhere).
--
-- ── Client-tooling prerequisites (macOS / Homebrew-cask gcloud) ──────────────
-- `gcloud sql connect` needs two things NOT bundled with the Homebrew-cask
-- google-cloud-sdk. `gcloud components install` is DISABLED for cask installs,
-- so install via Homebrew instead:
--
--   brew install cloud-sql-proxy        # fixes: "Cloud SQL Proxy (v2) not in PATH"
--   brew install libpq                  # fixes: "Psql client not found"
--   brew link --force libpq             # libpq is keg-only; puts psql on PATH
--
-- ── Password prompts ────────────────────────────────────────────────────────
-- This script's `\c appdb` reconnects after CREATE DATABASE, so psql prompts
-- for the **postgres ROOT** password TWICE (connect, then reconnect) — that is
-- the admin password set at `gcloud sql instances create --root-password`, NOT
-- FIVETRAN_DB_PW. To skip both prompts, export the root password first
-- (psql, spawned by `gcloud sql connect`, inherits it):
--
--   export PGPASSWORD='<postgres-root-password>'
--   sed "s/<FIVETRAN_DB_PASSWORD>/$FIVETRAN_DB_PW/" scripts/seed_test_db.sql \
--     | gcloud sql connect ftar-pg --user=postgres
--   unset PGPASSWORD
--
-- If you forgot the root password:
--   gcloud sql users set-password postgres --instance=ftar-pg --prompt-for-password

-- 1. Test database -----------------------------------------------------------
CREATE DATABASE appdb;
\c appdb

-- 2. Read-only Fivetran user (least privilege) -------------------------------
CREATE USER fivetran WITH PASSWORD '<FIVETRAN_DB_PASSWORD>';
GRANT CONNECT ON DATABASE appdb TO fivetran;
GRANT USAGE ON SCHEMA public TO fivetran;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO fivetran;
-- Future tables created during drift testing are auto-readable by fivetran:
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO fivetran;

-- 3. Seed tables -------------------------------------------------------------
-- Primary keys → clean Fivetran incremental (no _fivetran_id hashing).
-- updated_at → usable Query-Based incremental cursor.
-- These columns are deliberately chosen to exercise later drift scenarios
-- (rename customer_id, promote amount INT→TEXT, add/drop a column).

CREATE TABLE customers (
    customer_id  INT PRIMARY KEY,
    email        TEXT NOT NULL,
    created_at   TIMESTAMP NOT NULL DEFAULT now(),
    updated_at   TIMESTAMP NOT NULL DEFAULT now()
);
INSERT INTO customers (customer_id, email) VALUES
    (1, 'alice@example.com'),
    (2, 'bob@example.com'),
    (3, 'carol@example.com');

CREATE TABLE orders (
    order_id     INT PRIMARY KEY,
    customer_id  INT NOT NULL REFERENCES customers(customer_id),
    amount       NUMERIC(10,2) NOT NULL,
    status       TEXT NOT NULL,
    updated_at   TIMESTAMP NOT NULL DEFAULT now()
);
INSERT INTO orders (order_id, customer_id, amount, status) VALUES
    (1, 1, 99.50, 'paid'),
    (2, 2, 12.00, 'pending'),
    (3, 1, 250.00, 'paid');

-- Verify
\echo Seeded: customers / orders
SELECT 'customers' AS t, count(*) FROM customers
UNION ALL SELECT 'orders', count(*) FROM orders;
