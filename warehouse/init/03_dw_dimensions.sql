-- ============ DIMENSIONS ============

CREATE TABLE dw.dim_season (
    season_key      SERIAL PRIMARY KEY,
    competition     TEXT NOT NULL,           -- 'La Liga', 'Champions League'
    season_label    TEXT NOT NULL,           -- '2024/2025'
    start_date      DATE,
    end_date        DATE,
    UNIQUE (competition, season_label)
);

CREATE TABLE dw.dim_team (
    team_key        SERIAL PRIMARY KEY,
    team_name       TEXT NOT NULL UNIQUE,
    country         TEXT,
    statsbomb_id    INTEGER UNIQUE,
    fbref_id        TEXT,
    transfermarkt_id TEXT
);

CREATE TABLE dw.dim_player (
    player_key       SERIAL PRIMARY KEY,
    full_name        TEXT NOT NULL,
    known_as         TEXT,
    birth_date       DATE,
    nationality      TEXT,
    primary_position TEXT,                   -- GK, CB, FB, DM, CM, AM, W, ST
    preferred_foot   TEXT,
    height_cm        SMALLINT,
    statsbomb_id     INTEGER UNIQUE,
    fbref_id         TEXT,
    transfermarkt_id TEXT
);
CREATE INDEX idx_dim_player_name_trgm ON dw.dim_player USING gin (full_name gin_trgm_ops);

CREATE TABLE dw.dim_match (
    match_key        SERIAL PRIMARY KEY,
    season_key       INTEGER NOT NULL REFERENCES dw.dim_season,
    match_date       DATE NOT NULL,
    kickoff_utc      TIMESTAMPTZ,
    home_team_key    INTEGER NOT NULL REFERENCES dw.dim_team,
    away_team_key    INTEGER NOT NULL REFERENCES dw.dim_team,
    home_goals       SMALLINT,
    away_goals       SMALLINT,
    matchweek        SMALLINT,
    stadium          TEXT,
    referee          TEXT,
    statsbomb_id     INTEGER UNIQUE,
    fbref_id         TEXT
);
CREATE INDEX idx_dim_match_date ON dw.dim_match (match_date);

-- Contract / value history (slowly changing: one row per period)
CREATE TABLE dw.dim_player_contract (
    contract_key     SERIAL PRIMARY KEY,
    player_key       INTEGER NOT NULL REFERENCES dw.dim_player,
    team_key         INTEGER NOT NULL REFERENCES dw.dim_team,
    valid_from       DATE NOT NULL,
    valid_to         DATE,                   -- NULL = current
    contract_until   DATE,
    market_value_eur BIGINT,
    weekly_wage_eur  INTEGER
);
CREATE INDEX idx_contract_player ON dw.dim_player_contract (player_key, valid_from);
