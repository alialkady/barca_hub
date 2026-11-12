-- =====================================================================
-- Semantic views for the Ask Barca assistant (and for you: they are handy for ad-hoc SQL too).
-- They pre-join the star schema so common questions need ONE table instead of 4-5 joins. That makes the
-- model's SQL shorter (faster on a CPU) and removes whole classes of mistakes (missing joins, wrong keys).
--
-- Run (safe to repeat):
--   docker compose exec postgres psql -U barca -d barca_dw -v ON_ERROR_STOP=1 -f /models/semantic_views.sql
--
--   dw.v_player_match  one row per player per match, with names + all stats    (from fact_player_match_stats)
--   dw.v_shot          one row per shot, with names                            (from fact_shot)
--   dw.v_match         one row per match, with the Barcelona point of view     (from dim_match)
--   dw.search_key(t)   lower-case, accent-free text; player_search is built with it, so
--                      player_search LIKE '%pique%' finds 'Gerard Piqué'
-- =====================================================================

CREATE OR REPLACE FUNCTION dw.search_key(t text) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
    SELECT lower(translate(t, 'ÀÁÂÃÄÅÇÈÉÊËÌÍÎÏÑÒÓÔÕÖØÙÚÛÜÝàáâãäåçèéêëìíîïñòóôõöøùúûüýÿĀāĂăĄąĆćĈĉĊċČčĎďĐđĒēĔĕĖėĘęĚěĜĝĞğĠġĢģĤĥĨĩĪīĬĭĮįİıĴĵĶķĹĺĻļĽľŁłŃńŅņŇňŌōŎŏŐőŔŕŖŗŘřŚśŜŝŞşŠšŢţŤťŨũŪūŬŭŮůŰűŲųŴŵŶŷŸŹźŻżŽž', 'AAAAAACEEEEIIIINOOOOOOUUUUYaaaaaaceeeeiiiinoooooouuuuyyAaAaAaCcCcCcCcDdDdEeEeEeEeEeGgGgGgGgHhIiIiIiIiIiJjKkLlLlLlLlNnNnNnOoOoOoRrRrRrSsSsSsSsTtTtUuUuUuUuUuUuWwYyYZzZzZz'))
$$;

CREATE OR REPLACE VIEW dw.v_match AS
SELECT m.match_key,
       s.season_label                                  AS season,
       s.competition,
       m.match_date,
       m.matchweek,
       th.team_name                                    AS home_team,
       ta.team_name                                    AS away_team,
       m.home_goals,
       m.away_goals,
       m.stadium,
       m.referee,
       -- Barcelona's point of view (NULL for a match Barcelona did not play)
       CASE WHEN 'Barcelona' IN (th.team_name, ta.team_name) THEN th.team_name = 'Barcelona' END AS barca_home,
       CASE WHEN th.team_name = 'Barcelona' THEN ta.team_name
            WHEN ta.team_name = 'Barcelona' THEN th.team_name END                                 AS opponent,
       CASE WHEN th.team_name = 'Barcelona' THEN m.home_goals
            WHEN ta.team_name = 'Barcelona' THEN m.away_goals END                                 AS barca_goals,
       CASE WHEN th.team_name = 'Barcelona' THEN m.away_goals
            WHEN ta.team_name = 'Barcelona' THEN m.home_goals END                                 AS opponent_goals,
       CASE WHEN 'Barcelona' NOT IN (th.team_name, ta.team_name) THEN NULL
            WHEN (th.team_name = 'Barcelona' AND m.home_goals > m.away_goals)
              OR (ta.team_name = 'Barcelona' AND m.away_goals > m.home_goals) THEN 'W'
            WHEN m.home_goals = m.away_goals THEN 'D'
            ELSE 'L' END                                                                          AS result
FROM dw.dim_match m
JOIN dw.dim_season s  ON s.season_key  = m.season_key
JOIN dw.dim_team   th ON th.team_key   = m.home_team_key
JOIN dw.dim_team   ta ON ta.team_key   = m.away_team_key;

CREATE OR REPLACE VIEW dw.v_player_match AS
SELECT f.match_key,
       f.player_key,
       s.season_label                                  AS season,
       m.match_date,
       m.matchweek,
       COALESCE(p.known_as, p.full_name)               AS player,
       dw.search_key(p.full_name || ' ' || COALESCE(p.known_as, '')) AS player_search,
       t.team_name                                     AS team,
       CASE WHEN m.home_team_key = f.team_key THEN ta.team_name ELSE th.team_name END AS opponent,
       (m.home_team_key = f.team_key)                  AS is_home,
       f.position_played, f.minutes, f.started,
       f.goals, f.assists, f.shots, f.shots_on_target, f.xg, f.xa,
       f.passes_completed, f.passes_attempted, f.progressive_passes, f.progressive_carries,
       f.key_passes, f.dribbles_completed, f.tackles, f.interceptions, f.pressures, f.duels_won,
       f.yellow_cards, f.red_cards
FROM dw.fact_player_match_stats f
JOIN dw.dim_match  m  ON m.match_key  = f.match_key
JOIN dw.dim_season s  ON s.season_key = m.season_key
JOIN dw.dim_player p  ON p.player_key = f.player_key
JOIN dw.dim_team   t  ON t.team_key   = f.team_key
JOIN dw.dim_team   th ON th.team_key  = m.home_team_key
JOIN dw.dim_team   ta ON ta.team_key  = m.away_team_key;

CREATE OR REPLACE VIEW dw.v_shot AS
SELECT sh.shot_key,
       sh.match_key,
       sh.player_key,
       s.season_label                                  AS season,
       m.match_date,
       COALESCE(p.known_as, p.full_name)               AS player,
       dw.search_key(p.full_name || ' ' || COALESCE(p.known_as, '')) AS player_search,
       t.team_name                                     AS team,
       CASE WHEN m.home_team_key = sh.team_key THEN ta.team_name ELSE th.team_name END AS opponent,
       sh.minute, sh.shot_type, sh.body_part, sh.outcome,
       sh.is_goal,                                     -- TRUE when the shot was a goal: count GOALS with this
       sh.provider_xg                                  AS xg,
       sh.x, sh.y
FROM dw.fact_shot sh
JOIN dw.dim_match  m  ON m.match_key  = sh.match_key
JOIN dw.dim_season s  ON s.season_key = m.season_key
JOIN dw.dim_player p  ON p.player_key = sh.player_key
JOIN dw.dim_team   t  ON t.team_key   = sh.team_key
JOIN dw.dim_team   th ON th.team_key  = m.home_team_key
JOIN dw.dim_team   ta ON ta.team_key  = m.away_team_key;
