-- Raw landing tables: keep source payloads untouched so you can always rebuild dw.*
CREATE TABLE IF NOT EXISTS raw.ingestion_log (
    id           BIGSERIAL PRIMARY KEY,
    source       TEXT NOT NULL,              -- 'statsbomb', 'fbref', 'transfermarkt'
    dataset      TEXT NOT NULL,              -- 'matches', 'events', 'lineups', ...
    run_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    rows_loaded  INTEGER,
    notes        TEXT
);

CREATE TABLE IF NOT EXISTS raw.statsbomb_events (
    match_id     INTEGER NOT NULL,
    event_id     UUID NOT NULL,
    payload      JSONB NOT NULL,
    loaded_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (match_id, event_id)
);
CREATE INDEX IF NOT EXISTS idx_raw_sb_events_type
    ON raw.statsbomb_events ((payload->'type'->>'name'));
