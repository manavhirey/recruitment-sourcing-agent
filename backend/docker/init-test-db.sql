DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sourcing_api') THEN
        CREATE ROLE sourcing_api LOGIN PASSWORD 'replace-with-api-password';
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'sourcing_maintenance'
    ) THEN
        CREATE ROLE sourcing_maintenance LOGIN PASSWORD 'replace-with-maintenance-password';
    END IF;
END
$$;

CREATE DATABASE sourcing_test;
