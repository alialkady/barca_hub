-- ============ FACTS ============

-- One row per player per match (grain). Your main analytics table.
CREATE TABLE dw.fact_player_match_stats (
    match_key        INTEGER NOT NULL REFERENCES dw.dim_match,
    player_key       INTEGER NOT NULL REFERENCES dw.dim_player,
    team_key         INTEGER NOT NULL REFERENCES dw.dim_team,
    position_played  TEXT,
    minutes          SMALLINT NOT NULL DEFAULT 0,
    started          BOOLEAN,
    goals            SMALLINT DEFAULT 0,
    assists          SMALLINT DEFAULT 0,
    shots            SMALLINT DEFAULT 0,
    shots_on_target  SMALLINT DEFAULT 0,
    xg               NUMERIC(6,3),
    xa               NUMERIC(6,3),
    passes_completed SMALLINT,
    passes_attempted SMALLINT,
    progressive_passes SMALLINT,
    progressive_carries SMALLINT,
    key_passes       SMALLINT,
    dribbles_completed SMALLINT,
    tackles          SMALLINT,
    interceptions    SMALLINT,
    pressures        SMALLINT,
    duels_won        SMALLINT,
    yellow_cards     SMALLINT DEFAULT 0,
    red_cards        SMALLINT DEFAULT 0,
    PRIMARY KEY (match_key, player_key)
);
CREATE INDEX idx_fpms_player ON dw.fact_player_match_stats (player_key);

-- One row per shot. Training data for your own xG model.
CREATE TABLE dw.fact_shot (
    shot_key         BIGSERIAL PRIMARY KEY,
    match_key        INTEGER NOT NULL REFERENCES dw.dim_match,
    player_key       INTEGER NOT NULL REFERENCES dw.dim_player,
    team_key         INTEGER NOT NULL REFERENCES dw.dim_team,
    minute           SMALLINT NOT NULL,
    second           SMALLINT,
    x                NUMERIC(5,2),           -- pitch coords (StatsBomb: 0-120)
    y                NUMERIC(5,2),           -- (StatsBomb: 0-80)
    body_part        TEXT,                   -- 'Right Foot', 'Left Foot', 'Head'
    shot_type        TEXT,                   -- 'Open Play', 'Free Kick', 'Penalty'
    outcome          TEXT,                   -- 'Goal', 'Saved', 'Off T', ...
    is_goal          BOOLEAN NOT NULL,
    provider_xg      NUMERIC(5,4),           -- xG from the data provider
    model_xg         NUMERIC(5,4),           -- xG from YOUR model (filled later)
    source_event_id  UUID UNIQUE
);
CREATE INDEX idx_shot_match ON dw.fact_shot (match_key);

-- Season-level player rollup (rebuilt by dbt later; table now for early dashboards)
CREATE TABLE dw.fact_player_season_stats (
    season_key       INTEGER NOT NULL REFERENCES dw.dim_season,
    player_key       INTEGER NOT NULL REFERENCES dw.dim_player,
    team_key         INTEGER NOT NULL REFERENCES dw.dim_team,
    matches          SMALLINT,
    minutes          INTEGER,
    stats_per90      JSONB,                  -- {"xg":0.42,"progressive_passes":6.1,...}
    PRIMARY KEY (season_key, player_key, team_key)
);

-- Player embeddings for similarity search (transfer scouting)
CREATE TABLE dw.player_embedding (
    player_key       INTEGER NOT NULL REFERENCES dw.dim_player,
    season_key       INTEGER NOT NULL REFERENCES dw.dim_season,
    embedding        vector(32),             -- size will change with your feature set
    model_version    TEXT NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (player_key, season_key, model_version)
);
