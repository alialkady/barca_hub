-- OPTIONAL HARDENING for the Ask Barça assistant.
-- Creates a database role that can ONLY read the dw tables and views the assistant may query.
-- Run semantic_views.sql FIRST (or run this script again after it). Even if a bad query
-- got past the SQL guard, this role cannot write, cannot read raw.* / live.*, and is not a superuser
-- (so file-reading functions like pg_read_file are refused too).
--
-- Run once (the file is mounted at /models):
--   docker compose exec postgres psql -U barca -d barca_dw -v ON_ERROR_STOP=1 -f /models/assistant_readonly_role.sql
-- Then add to .env:   ASSISTANT_DB_USER=assistant_ro   ASSISTANT_DB_PASSWORD=assistant_ro_pw
-- and restart the assistant:   docker compose --profile assistant up -d assistant
-- (Dev password below; change it in both places for anything beyond your laptop.)

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'assistant_ro') THEN
        CREATE ROLE assistant_ro LOGIN PASSWORD 'assistant_ro_pw'
            NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
    END IF;
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO assistant_ro', current_database());
END
$$;

GRANT USAGE ON SCHEMA dw TO assistant_ro;
GRANT SELECT ON dw.dim_player, dw.dim_team, dw.dim_season, dw.dim_match,
                dw.fact_player_match_stats, dw.fact_shot TO assistant_ro;

-- the semantic views the assistant actually queries (create them first: /models/semantic_views.sql)
DO $$
BEGIN
    IF to_regclass('dw.v_match') IS NOT NULL THEN
        GRANT SELECT ON dw.v_player_match, dw.v_shot, dw.v_match TO assistant_ro;
    ELSE
        RAISE NOTICE 'dw.v_match does not exist yet: run semantic_views.sql, then run this script again';
    END IF;
END
$$;

ALTER ROLE assistant_ro SET default_transaction_read_only = on;
ALTER ROLE assistant_ro SET statement_timeout = '8s';
ALTER ROLE assistant_ro CONNECTION LIMIT 10;
