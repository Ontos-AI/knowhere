-- Local development only. Production passwords belong in a secret manager.
-- Root owns migrations; runtime processes receive DML without bypassing RLS.
DO $bootstrap$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'knowhere_runtime') THEN
        CREATE ROLE knowhere_runtime LOGIN PASSWORD 'runtime123'
            NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
    END IF;
END
$bootstrap$;

ALTER ROLE knowhere_runtime NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
GRANT CONNECT ON DATABASE "Knowhere" TO knowhere_runtime;
GRANT USAGE ON SCHEMA public TO knowhere_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO knowhere_runtime;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO knowhere_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE root IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO knowhere_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE root IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO knowhere_runtime;
