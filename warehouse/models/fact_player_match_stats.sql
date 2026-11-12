-- =====================================================================
-- dw.fact_player_match_stats  (grain: one row per player per match)
-- Built from raw.statsbomb_events only. Safe to re-run: it rebuilds the
-- rows of every StatsBomb match in one transaction.
--
-- Run:  docker compose exec postgres psql -U barca -d barca_dw \
--         -v ON_ERROR_STOP=1 -f /models/fact_player_match_stats.sql
--
-- DEFINITIONS (read these before trusting a number)
--  minutes      90 for a full match (120 if extra time was played), NOT including
--               stoppage time. Starters begin at 0; subs begin at the substitution
--               minute. A player ends at: being subbed off, a red / second yellow,
--               or the end of the match. A late sub can have 0 minutes.
--  shots/xg     Shot events, excluding penalty shoot-outs (period 5). xg = StatsBomb xG.
--  shots_on_target  outcome is Goal, Saved or Saved to Post.
--  assists      passes flagged goal_assist by StatsBomb.
--  key_passes   passes flagged shot_assist or goal_assist (StatsBomb never sets both).
--  xa           sum of the xG of shots that the player's pass directly set up.
--  passes_*     all Pass events, minus 'Injury Clearance'. Completed = no outcome recorded.
--  progressive_passes / progressive_carries  SIMPLIFIED rule, not FBref's exact one:
--               completed pass (or any carry) that ENDS outside the team's own
--               defensive 40% (x >= 48 on StatsBomb's 120x80 pitch) AND moves the ball
--               at least 10 yards closer to the centre of the goal line.
--               Kick-offs, goal kicks and corners are ignored.
--  tackles      Duel events of type 'Tackle' (all attempts, won or lost).
--  duels_won / interceptions   outcomes Won, Success In Play, Success Out.
--  pressures    Pressure events.
--  yellow_cards 'Yellow Card' + 'Second Yellow'.   red_cards 'Red Card' + 'Second Yellow'.
--  position_played  the position the player was most often recorded in, that match
--               (StatsBomb position names, e.g. 'Left Wing').
-- =====================================================================
BEGIN;

-- 1) One narrow, typed row per relevant event (read the big JSONB table once)
CREATE TEMP TABLE _ev ON COMMIT DROP AS
SELECT
    e.match_id,
    e.event_id,
    e.payload->'type'->>'name'                         AS type,
    (e.payload->'player'->>'id')::int                  AS player_id,
    (e.payload->'team'->>'id')::int                    AS team_id,
    (e.payload->>'minute')::int                        AS minute,
    (e.payload->>'period')::int                        AS period,
    e.payload->'position'->>'name'                     AS position,
    (e.payload->'location'->>0)::numeric               AS x,
    (e.payload->'location'->>1)::numeric               AS y,
    COALESCE(e.payload->'pass'->'end_location'->>0,
             e.payload->'carry'->'end_location'->>0)::numeric AS ex,
    COALESCE(e.payload->'pass'->'end_location'->>1,
             e.payload->'carry'->'end_location'->>1)::numeric AS ey,
    e.payload->'pass'->'outcome'->>'name'              AS pass_outcome,
    e.payload->'pass'->'type'->>'name'                 AS pass_type,
    COALESCE((e.payload->'pass'->>'goal_assist')::boolean, false) AS goal_assist,
    COALESCE((e.payload->'pass'->>'shot_assist')::boolean, false) AS shot_assist,
    e.payload->'duel'->'type'->>'name'                 AS duel_type,
    e.payload->'duel'->'outcome'->>'name'              AS duel_outcome,
    e.payload->'dribble'->'outcome'->>'name'           AS dribble_outcome,
    e.payload->'interception'->'outcome'->>'name'      AS interception_outcome,
    COALESCE(e.payload->'foul_committed'->'card'->>'name',
             e.payload->'bad_behaviour'->'card'->>'name') AS card,
    e.payload->'substitution'->'replacement'->>'id'    AS sub_on_id,
    e.payload->'shot'->'outcome'->>'name'              AS shot_outcome,
    (e.payload->'shot'->>'statsbomb_xg')::numeric      AS shot_xg,
    (e.payload->'shot'->>'key_pass_id')::uuid          AS key_pass_id
FROM raw.statsbomb_events e
WHERE e.payload->'type'->>'name' IN
      ('Pass','Carry','Dribble','Duel','Interception','Pressure','Shot',
       'Foul Committed','Bad Behaviour','Substitution','Half End');

-- yards the ball moves toward the centre of the goal line (120, 40)
ALTER TABLE _ev ADD COLUMN gain_yards numeric;
UPDATE _ev
SET gain_yards = sqrt(power(120 - x, 2)  + power(40 - y, 2))
               - sqrt(power(120 - ex, 2) + power(40 - ey, 2))
WHERE type IN ('Pass','Carry') AND x IS NOT NULL AND ex IS NOT NULL;

CREATE INDEX ON _ev (match_id, player_id);
ANALYZE _ev;

-- 2) Who played, from when to when
CREATE TEMP TABLE _app ON COMMIT DROP AS
WITH starters AS (
    SELECT e.match_id,
           (l->'player'->>'id')::int         AS player_id,
           (e.payload->'team'->>'id')::int   AS team_id
    FROM raw.statsbomb_events e
    CROSS JOIN LATERAL jsonb_array_elements(e.payload->'tactics'->'lineup') AS l
    WHERE e.payload->'type'->>'name' = 'Starting XI'
),
apps AS (
    SELECT match_id, player_id, team_id, true AS started, 0 AS start_min FROM starters
    UNION ALL
    SELECT match_id, sub_on_id::int, team_id, false, minute
    FROM _ev WHERE type = 'Substitution'
),
match_len AS (
    SELECT match_id,
           CASE WHEN max(period) FILTER (WHERE period <= 4) >= 3 THEN 120 ELSE 90 END AS len
    FROM _ev WHERE type = 'Half End' GROUP BY match_id
),
subbed_off AS (
    SELECT match_id, player_id, min(minute) AS off_min
    FROM _ev WHERE type = 'Substitution' GROUP BY 1, 2
),
sent_off AS (
    SELECT match_id, player_id, min(minute) AS off_min
    FROM _ev WHERE card IN ('Red Card', 'Second Yellow') GROUP BY 1, 2
)
SELECT a.match_id, a.player_id, a.team_id, a.started,
       GREATEST(a.start_min,
                LEAST(COALESCE(l.len, 90), COALESCE(so.off_min, 999), COALESCE(sd.off_min, 999)))
         - a.start_min AS minutes
FROM apps a
LEFT JOIN match_len  l  ON l.match_id  = a.match_id
LEFT JOIN subbed_off so ON so.match_id = a.match_id AND so.player_id = a.player_id
LEFT JOIN sent_off   sd ON sd.match_id = a.match_id AND sd.player_id = a.player_id;

-- 3) Event counts per player per match
CREATE TEMP TABLE _agg ON COMMIT DROP AS
SELECT match_id, player_id,
    count(*) FILTER (WHERE type='Shot' AND period <> 5)                                   AS shots,
    count(*) FILTER (WHERE type='Shot' AND period <> 5 AND shot_outcome = 'Goal')         AS goals,
    count(*) FILTER (WHERE type='Shot' AND period <> 5
                       AND shot_outcome IN ('Goal','Saved','Saved to Post'))              AS shots_on_target,
    COALESCE(sum(shot_xg) FILTER (WHERE type='Shot' AND period <> 5), 0)                  AS xg,
    count(*) FILTER (WHERE type='Pass' AND pass_outcome IS DISTINCT FROM 'Injury Clearance')      AS passes_attempted,
    count(*) FILTER (WHERE type='Pass' AND pass_outcome IS NULL)                                  AS passes_completed,
    count(*) FILTER (WHERE type='Pass' AND goal_assist)                                           AS assists,
    count(*) FILTER (WHERE type='Pass' AND (goal_assist OR shot_assist))                          AS key_passes,
    count(*) FILTER (WHERE type='Pass' AND pass_outcome IS NULL AND ex >= 48 AND gain_yards >= 10
                       AND pass_type IS DISTINCT FROM 'Kick Off'
                       AND pass_type IS DISTINCT FROM 'Goal Kick'
                       AND pass_type IS DISTINCT FROM 'Corner')                                   AS progressive_passes,
    count(*) FILTER (WHERE type='Carry' AND ex >= 48 AND gain_yards >= 10)                        AS progressive_carries,
    count(*) FILTER (WHERE type='Dribble' AND dribble_outcome = 'Complete')               AS dribbles_completed,
    count(*) FILTER (WHERE type='Duel' AND duel_type = 'Tackle')                          AS tackles,
    count(*) FILTER (WHERE type='Interception'
                       AND interception_outcome IN ('Won','Success In Play','Success Out')) AS interceptions,
    count(*) FILTER (WHERE type='Pressure')                                               AS pressures,
    count(*) FILTER (WHERE type='Duel'
                       AND duel_outcome IN ('Won','Success In Play','Success Out'))       AS duels_won,
    count(*) FILTER (WHERE card IN ('Yellow Card','Second Yellow'))                       AS yellow_cards,
    count(*) FILTER (WHERE card IN ('Red Card','Second Yellow'))                          AS red_cards
FROM _ev
WHERE player_id IS NOT NULL
GROUP BY match_id, player_id;

-- 4) Expected assists: xG of the shots each passer set up
CREATE TEMP TABLE _xa ON COMMIT DROP AS
SELECT p.match_id, p.player_id, sum(s.shot_xg) AS xa
FROM _ev s
JOIN _ev p ON p.type = 'Pass' AND p.match_id = s.match_id AND p.event_id = s.key_pass_id
WHERE s.type = 'Shot' AND s.period <> 5
GROUP BY p.match_id, p.player_id;

-- 5) Most frequent position per player per match
CREATE TEMP TABLE _pos ON COMMIT DROP AS
SELECT match_id, player_id, mode() WITHIN GROUP (ORDER BY position) AS position_played
FROM _ev WHERE player_id IS NOT NULL AND position IS NOT NULL
GROUP BY match_id, player_id;

-- 6) Rebuild the fact table (only StatsBomb-sourced matches)
DELETE FROM dw.fact_player_match_stats
WHERE match_key IN (SELECT match_key FROM dw.dim_match WHERE statsbomb_id IS NOT NULL);

INSERT INTO dw.fact_player_match_stats (
    match_key, player_key, team_key, position_played, minutes, started,
    goals, assists, shots, shots_on_target, xg, xa,
    passes_completed, passes_attempted, progressive_passes, progressive_carries,
    key_passes, dribbles_completed, tackles, interceptions, pressures, duels_won,
    yellow_cards, red_cards)
SELECT
    m.match_key, p.player_key, t.team_key, pos.position_played, a.minutes, a.started,
    COALESCE(g.goals, 0), COALESCE(g.assists, 0), COALESCE(g.shots, 0), COALESCE(g.shots_on_target, 0),
    COALESCE(g.xg, 0), COALESCE(xa.xa, 0),
    COALESCE(g.passes_completed, 0), COALESCE(g.passes_attempted, 0),
    COALESCE(g.progressive_passes, 0), COALESCE(g.progressive_carries, 0),
    COALESCE(g.key_passes, 0), COALESCE(g.dribbles_completed, 0), COALESCE(g.tackles, 0),
    COALESCE(g.interceptions, 0), COALESCE(g.pressures, 0), COALESCE(g.duels_won, 0),
    COALESCE(g.yellow_cards, 0), COALESCE(g.red_cards, 0)
FROM _app a
JOIN dw.dim_match  m ON m.statsbomb_id = a.match_id
JOIN dw.dim_player p ON p.statsbomb_id = a.player_id
JOIN dw.dim_team   t ON t.statsbomb_id = a.team_id
LEFT JOIN _agg g   ON g.match_id   = a.match_id AND g.player_id   = a.player_id
LEFT JOIN _xa  xa  ON xa.match_id  = a.match_id AND xa.player_id  = a.player_id
LEFT JOIN _pos pos ON pos.match_id = a.match_id AND pos.player_id = a.player_id;

-- 7) Make sure nobody was silently dropped by the joins
DO $$
DECLARE expected int; inserted int;
BEGIN
    SELECT count(*) INTO expected FROM _app;
    SELECT count(*) INTO inserted FROM dw.fact_player_match_stats f
      JOIN dw.dim_match m USING (match_key) WHERE m.statsbomb_id IS NOT NULL;
    IF expected <> inserted THEN
        RAISE WARNING 'fact_player_match_stats: % appearances found but % rows inserted', expected, inserted;
    ELSE
        RAISE NOTICE 'fact_player_match_stats: % rows built', inserted;
    END IF;
END $$;

COMMIT;
