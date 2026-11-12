-- Sanity checks for dw.fact_player_match_stats. Every check should return 0 problem rows
-- (except the informational ones at the end).
-- Run: docker compose exec postgres psql -U barca -d barca_dw -f /models/fact_player_match_stats_checks.sql

\echo '1) team-matches without exactly 11 starters (expect 0 rows)'
SELECT m.match_date, t.team_name, count(*) FILTER (WHERE f.started) AS starters
FROM dw.fact_player_match_stats f
JOIN dw.dim_match m USING (match_key) JOIN dw.dim_team t USING (team_key)
GROUP BY m.match_key, m.match_date, t.team_name
HAVING count(*) FILTER (WHERE f.started) <> 11;

\echo '2) team minutes should be 990 (11 x 90) unless a player was sent off (expect 0 rows)'
SELECT m.match_date, t.team_name, sum(f.minutes) AS team_minutes, sum(f.red_cards) AS reds
FROM dw.fact_player_match_stats f
JOIN dw.dim_match m USING (match_key) JOIN dw.dim_team t USING (team_key)
GROUP BY m.match_key, m.match_date, t.team_name
HAVING sum(f.minutes) <> 990 AND sum(f.red_cards) = 0;

\echo '3) goals/xG per player-match must equal dw.fact_shot (expect 0 rows)'
SELECT f.match_key, f.player_key, f.goals, s.goals AS shot_goals, f.shots, s.shots AS shot_rows
FROM dw.fact_player_match_stats f
LEFT JOIN (SELECT match_key, player_key, count(*) AS shots, count(*) FILTER (WHERE is_goal) AS goals
           FROM dw.fact_shot GROUP BY 1, 2) s USING (match_key, player_key)
WHERE f.goals <> COALESCE(s.goals, 0) OR f.shots <> COALESCE(s.shots, 0);

\echo '4) shooters in fact_shot with no appearance row (expect 0 rows)'
SELECT DISTINCT s.match_key, s.player_key
FROM dw.fact_shot s
LEFT JOIN dw.fact_player_match_stats f USING (match_key, player_key)
WHERE f.match_key IS NULL;

\echo '5) impossible values (expect 0 rows)'
SELECT * FROM dw.fact_player_match_stats
WHERE passes_completed > passes_attempted OR shots_on_target > shots OR goals > shots
   OR minutes NOT BETWEEN 0 AND 120 OR progressive_passes > passes_completed;

\echo '6) INFO: assists on the fact table vs goals scored by the same team (assists should be <= goals)'
SELECT t.team_name, sum(f.goals) AS goals, sum(f.assists) AS assists
FROM dw.fact_player_match_stats f JOIN dw.dim_team t USING (team_key)
WHERE t.team_name = 'Barcelona' GROUP BY 1;
