DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sourcing_api') THEN
        CREATE ROLE sourcing_api LOGIN PASSWORD 'replace-with-api-password';
    END IF;
END
$$;

CREATE DATABASE sourcing_test;
