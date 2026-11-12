-- Written by stream consumers while a match is running.

CREATE TABLE live.match_state (
    match_id         INTEGER NOT NULL,       -- source match id
    minute           SMALLINT NOT NULL,
    home_goals       SMALLINT NOT NULL DEFAULT 0,
    away_goals       SMALLINT NOT NULL DEFAULT 0,
    home_xg          NUMERIC(5,3) NOT NULL DEFAULT 0,
    away_xg          NUMERIC(5,3) NOT NULL DEFAULT 0,
    home_possession  NUMERIC(4,1),
    home_shots       SMALLINT NOT NULL DEFAULT 0,
    away_shots       SMALLINT NOT NULL DEFAULT 0,
    home_pressures   SMALLINT,
    away_pressures   SMALLINT,
    momentum         NUMERIC(5,2),           -- + favours home, - favours away
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (match_id, minute)
);

CREATE TABLE live.event_feed (
    match_id         INTEGER NOT NULL,
    event_id         UUID NOT NULL,
    minute           SMALLINT NOT NULL,
    second           SMALLINT,
    event_type       TEXT NOT NULL,
    team             TEXT,
    player           TEXT,
    detail           JSONB,
    received_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (match_id, event_id)
);
