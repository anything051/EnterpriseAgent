\set ON_ERROR_STOP on
\set reader_password ''
\getenv reader_password ENTERPRISE_DB_READER_PASSWORD

BEGIN;
CREATE SCHEMA IF NOT EXISTS enterprise_demo;

CREATE TABLE IF NOT EXISTS enterprise_demo.sales (
    order_id text PRIMARY KEY,
    sales_day date NOT NULL,
    region text NOT NULL,
    sku text NOT NULL,
    units integer NOT NULL CHECK (units > 0),
    unit_price_cents integer NOT NULL CHECK (unit_price_cents >= 0),
    snapshot text NOT NULL CHECK (snapshot = 'qh-2026-06-v1')
);

INSERT INTO enterprise_demo.sales (order_id, sales_day, region, sku, units, unit_price_cents, snapshot) VALUES
    ('QH-001', '2026-06-01', '华东', 'A100', 7, 10000, 'qh-2026-06-v1'),
    ('QH-002', '2026-06-01', '华东', 'B200', 5, 15000, 'qh-2026-06-v1'),
    ('QH-003', '2026-06-01', '华南', 'A100', 3, 10000, 'qh-2026-06-v1'),
    ('QH-004', '2026-06-02', '华东', 'B200', 4, 15000, 'qh-2026-06-v1'),
    ('QH-005', '2026-06-02', '华南', 'C300', 6, 8000, 'qh-2026-06-v1')
ON CONFLICT (order_id) DO NOTHING;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'enterprise_reader') THEN
        CREATE ROLE enterprise_reader NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
    END IF;
END
$$;
ALTER ROLE enterprise_reader NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
ALTER ROLE enterprise_reader SET default_transaction_read_only = on;
ALTER ROLE enterprise_reader SET statement_timeout = '3s';
GRANT CONNECT ON DATABASE enterprise_rag TO enterprise_reader;
GRANT USAGE ON SCHEMA enterprise_demo TO enterprise_reader;
GRANT SELECT ON enterprise_demo.sales TO enterprise_reader;

SELECT length(:'reader_password') >= 24 AS configure_reader \gset
\if :configure_reader
ALTER ROLE enterprise_reader LOGIN PASSWORD :'reader_password';
\endif
COMMIT;
