"""The semantic views must agree with the star schema, and answer every golden question."""
import json
import os
import sys

from conftest import needs_db

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "eval"))
from run_eval import HERE, compare  # noqa: E402

from db import run_select  # noqa: E402
from guard import validate  # noqa: E402


def one(dsn, sql):
    return run_select(dsn, validate(sql))[1][0][0]


@needs_db
def test_row_counts_and_totals_match_the_base_tables(dsn):
    for view, base in [("v_match", "dim_match"), ("v_player_match", "fact_player_match_stats"), ("v_shot", "fact_shot")]:
        assert one(dsn, f"SELECT COUNT(*) FROM dw.{view}") == one(dsn, f"SELECT COUNT(*) FROM dw.{base}")
    assert one(dsn, "SELECT SUM(goals) FROM dw.v_player_match") == one(dsn, "SELECT SUM(goals) FROM dw.fact_player_match_stats")
    assert one(dsn, "SELECT COUNT(*) FILTER (WHERE is_goal) FROM dw.v_shot") == one(dsn, "SELECT COUNT(*) FILTER (WHERE is_goal) FROM dw.fact_shot")


@needs_db
def test_search_key_ignores_accents_and_case(dsn):
    key = lambda t: run_select(dsn, f"SELECT dw.search_key('{t}')")[1][0][0]
    assert key("Gerard Piqué") == "gerard pique" and key("Ivan Rakitić") == "ivan rakitic"
    assert key("Filip Malbašić") == "filip malbasic" and key("Søren Ørsted") == "soren orsted"
    assert one(dsn, "SELECT COUNT(*) FROM dw.v_player_match WHERE player_search LIKE '%pique%'") > 0


@needs_db
def test_barcelona_perspective_is_consistent(dsn):
    assert one(dsn, "SELECT COUNT(*) FROM dw.v_match WHERE barca_goals + opponent_goals <> home_goals + away_goals") == 0
    assert one(dsn, "SELECT COUNT(*) FROM dw.v_match WHERE result IS NOT NULL AND result NOT IN ('W','D','L')") == 0
    assert one(dsn, "SELECT COUNT(*) FROM dw.v_match WHERE result = 'W' AND barca_goals <= opponent_goals") == 0
    assert one(dsn, "SELECT COUNT(*) FROM dw.v_match WHERE result = 'L' AND barca_goals >= opponent_goals") == 0
    assert one(dsn, "SELECT COUNT(*) FROM dw.v_player_match WHERE team = opponent") == 0
    assert one(dsn, "SELECT COUNT(*) FROM dw.v_shot WHERE team = opponent") == 0


@needs_db
def test_every_golden_question_is_answerable_through_the_views(dsn):
    golden = [g for g in json.load(open(os.path.join(HERE, "golden.json"))) if g["expect"] == "result"]
    assert golden and all(g.get("sql_views") for g in golden)
    for g in golden:
        _, ref, _ = run_select(dsn, validate(g["sql"]))
        _, got, _ = run_select(dsn, validate(g["sql_views"]))
        ok, why = compare(ref, got, g.get("ordered", False))
        assert ok, f"{g['id']}: {why}"
