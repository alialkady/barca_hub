import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from guard import UnsafeSQL, validate  # noqa: E402

OK = [
    "SELECT SUM(goals) FROM dw.v_player_match WHERE player_search LIKE '%messi%'",
    "SELECT COUNT(*) FILTER (WHERE is_goal) FROM dw.v_shot",
    "SELECT result, COUNT(*) FROM dw.v_match GROUP BY result",
    "SELECT COUNT(*) FROM dw.dim_match",
    "select * from dw.dim_player where full_name ilike '%messi%' limit 5;",
    "WITH b AS (SELECT * FROM dw.dim_match) SELECT COUNT(*) FROM b",
    "SELECT SUM(goals) FILTER (WHERE minutes > 60) FROM dw.fact_player_match_stats",
    "SELECT 1 AS x UNION ALL SELECT 2",
    "SELECT round(90.0 * SUM(xg) / NULLIF(SUM(minutes), 0), 2) FROM dw.fact_player_match_stats",
    "SELECT * FROM generate_series(1, 3) AS g(n)",
    "SELECT m.match_date, th.team_name FROM dw.dim_match m JOIN dw.dim_team th ON th.team_key = m.home_team_key",
    "SELECT 1 -- trailing comment",
]

BAD = [
    ("", "empty"),
    ("DROP TABLE dw.dim_player", "Only SELECT"),
    ("DELETE FROM dw.dim_player", "Only SELECT"),
    ("UPDATE dw.dim_player SET full_name = 'x'", "Only SELECT"),
    ("INSERT INTO dw.dim_team (team_name) VALUES ('x')", "Only SELECT"),
    ("SELECT 1; DROP TABLE dw.dim_player", "Exactly one"),
    ("SELECT * INTO dw.copy_of_players FROM dw.dim_player", "INTO"),
    ("SELECT * FROM dw.dim_player FOR UPDATE", "LOCK"),
    ("SELECT * FROM raw.statsbomb_events", "not available"),
    ("SELECT * FROM live.match_state", "not available"),
    ("SELECT * FROM pg_catalog.pg_user", "not available"),
    ("SELECT * FROM information_schema.tables", "not available"),
    ("SELECT * FROM dim_player", "not available"),
    ("SELECT * FROM dw.player_embedding", "not available"),
    ("SELECT * FROM otherdb.dw.dim_player", "not available"),
    ("SELECT pg_read_file('/etc/passwd')", "not allowed"),
    ("SELECT pg_sleep(100)", "not allowed"),
    ("SELECT set_config('x', 'y', false)", "not allowed"),
    ("SELECT lo_import('/etc/passwd')", "not allowed"),
    ("SELECT current_setting('server_version')", "not allowed"),
    ("SELECT * FROM dw.dim_player WHERE player_key IN (SELECT pg_sleep(1)::int)", "not allowed"),
    ("COPY dw.dim_player TO '/tmp/x'", "Only SELECT"),
    ("SET statement_timeout = 0", "Only SELECT"),
    ("SELECT FROM WHERE (((", "parsed"),
]


@pytest.mark.parametrize("sql", OK)
def test_allowed(sql):
    assert validate(sql)


@pytest.mark.parametrize("sql,fragment", BAD)
def test_blocked(sql, fragment):
    with pytest.raises(UnsafeSQL) as e:
        validate(sql)
    assert fragment.lower() in str(e.value).lower()


def test_trailing_semicolon_is_stripped():
    assert validate("SELECT 1;") == "SELECT 1"
