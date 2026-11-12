import pytest
from conftest import FakeLLM, needs_db

from assistant import Assistant, estimate_tokens, extract_sql, template_answer
from db import run_select
from llm_client import LLMError

GOOD_SQL = "```sql\nSELECT COUNT(*) AS n FROM dw.v_match\n```"


# ---------------------------------------------------------------- pure helpers
@pytest.mark.parametrize("reply,expected", [
    ("```sql\nSELECT 1\n```", ("SELECT 1", None)),
    ("Sure!\n```SQL\nSELECT 1\n```\nHope that helps", ("SELECT 1", None)),
    ("```sql\nSELECT 1", ("SELECT 1", None)),                       # model got cut off before the closing fence
    ("SELECT 1", ("SELECT 1", None)),
    ("WITH a AS (SELECT 1) SELECT * FROM a", ("WITH a AS (SELECT 1) SELECT * FROM a", None)),
    ("CANNOT_ANSWER: transfers are not in the data", (None, "transfers are not in the data")),
    ("```sql\nCANNOT_ANSWER: no such table\n```", (None, "no such table")),
    ("I think the answer is 42.", (None, None)),
])
def test_extract_sql(reply, expected):
    assert extract_sql(reply) == expected


def test_template_answer():
    assert template_answer(["player", "goals"], [["Lionel Messi", 30]]) == "player: Lionel Messi, goals: 30"
    assert template_answer(["n"], [[35]]) == "n: 35"
    multi = template_answer(["player", "xg"], [["A", 1.5], ["B", 1.2]])
    assert multi == "2 rows. First row: player: A, xg: 1.5"
    assert "more" in template_answer(["a"], [[1]] * 3, truncated=True)
    assert template_answer(["progressive_passes_per90"], [[13.5]]) == "progressive passes per90: 13.5"


def test_estimate_tokens_is_proportional():
    assert estimate_tokens([{"role": "user", "content": "x" * 300}]) == 100


# ---------------------------------------------------------------- pipeline (needs the warehouse)
@needs_db
def test_default_makes_a_single_model_call(dsn):
    llm = FakeLLM([GOOD_SQL, "this second reply must never be requested"])
    out = Assistant(llm, dsn).ask("How many matches are there?")
    assert len(llm.calls) == 1
    assert out["answer"].startswith("n: ") and out["sql"].startswith("SELECT COUNT(*)")
    assert out["columns"] == ["n"] and len(out["rows"]) == 1 and out["attempts"] == []


@needs_db
def test_phrasing_is_opt_in_and_only_sees_the_result_table(dsn):
    llm = FakeLLM([GOOD_SQL, "There are some matches."])
    out = Assistant(llm, dsn, phrase_answers=True).ask("How many matches are there?")
    assert out["answer"] == "There are some matches." and len(llm.calls) == 2
    prompt = llm.calls[1][-1]["content"]
    assert "| n |" in prompt and "How many matches" in prompt


@needs_db
def test_per_call_override_of_phrasing(dsn):
    llm = FakeLLM([GOOD_SQL, "unused"])
    Assistant(llm, dsn, phrase_answers=True).ask("q", phrase=False)
    assert len(llm.calls) == 1


@needs_db
def test_repairs_a_failing_query(dsn):
    llm = FakeLLM(["```sql\nSELECT nope FROM dw.v_match\n```", GOOD_SQL])
    out = Assistant(llm, dsn).ask("How many matches?")
    assert out["sql"].startswith("SELECT COUNT(*)") and len(out["attempts"]) == 1
    assert "nope" in out["attempts"][0]["error"]
    feedback = llm.calls[1][-1]["content"]                            # the error was sent back to the model
    assert "did not work" in feedback and "nope" in feedback
    assert "ALL the views, columns and values" in feedback                   # 'does not exist' -> schema is re-sent


@needs_db
def test_invented_table_gets_the_schema_reminder(dsn):
    llm = FakeLLM(["```sql\nSELECT stadium FROM dw.dim_stadium\n```", GOOD_SQL])
    out = Assistant(llm, dsn).ask("Which stadium?")
    feedback = llm.calls[1][-1]["content"]
    assert "dim_stadium" in feedback and "not available" in feedback
    assert "dw.v_match" in feedback and "stadium" in feedback
    assert out["sql"] is not None


@needs_db
def test_a_plain_failure_does_not_resend_the_schema(dsn):
    llm = FakeLLM(["no sql here at all", GOOD_SQL])
    Assistant(llm, dsn).ask("hi")
    assert "ALL the views" not in llm.calls[1][-1]["content"]


@needs_db
def test_unsafe_sql_is_rejected_and_never_runs(dsn):
    llm = FakeLLM(["```sql\nDROP TABLE dw.dim_player\n```", GOOD_SQL])
    out = Assistant(llm, dsn).ask("drop everything")
    assert len(out["attempts"]) == 1 and "Only SELECT" in out["attempts"][0]["error"]
    _, rows, _ = run_select(dsn, "SELECT COUNT(*) FROM dw.dim_player")
    assert rows[0][0] > 0                                             # table still there


@needs_db
def test_gives_up_after_max_attempts(dsn):
    bad = "```sql\nSELECT nope FROM dw.v_match\n```"
    out = Assistant(FakeLLM([bad, bad]), dsn).ask("???")
    assert out["sql"] is None and len(out["attempts"]) == 2 and "couldn't" in out["answer"]


@needs_db
def test_refusal(dsn):
    out = Assistant(FakeLLM(["CANNOT_ANSWER: transfers are not in the data"]), dsn).ask("Who should we sign?")
    assert out["sql"] is None and "transfers are not in the data" in out["answer"]


EMPTY_SQL = "```sql\nSELECT * FROM dw.v_player_match WHERE position_played = 'GK'\n```"
FIXED_SQL = "```sql\nSELECT COUNT(*) AS n FROM dw.v_player_match WHERE position_played = 'Goalkeeper'\n```"


@needs_db
def test_empty_result_gets_one_retry_with_the_valid_values(dsn):
    llm = FakeLLM([EMPTY_SQL, FIXED_SQL])
    out = Assistant(llm, dsn).ask("How many goalkeeper appearances?")
    assert out["rows"] and out["rows"][0][0] > 0 and "Goalkeeper" in out["sql"]
    assert out["attempts"][0]["error"] == "the query returned no rows"
    feedback = llm.calls[1][-1]["content"]
    assert "returned no rows" in feedback and "'Goalkeeper'" in feedback and "return the same query again" in feedback


@needs_db
def test_empty_twice_stands_as_an_empty_answer_without_a_phrasing_call(dsn):
    llm = FakeLLM([EMPTY_SQL, EMPTY_SQL, "must not be requested"])
    out = Assistant(llm, dsn, phrase_answers=True).ask("Stats for a position nobody plays")
    assert out["rows"] == [] and out["sql"] is not None and "no rows" in out["answer"] and len(llm.calls) == 2


@needs_db
def test_a_broken_retry_still_returns_the_earlier_empty_result(dsn):
    llm = FakeLLM([EMPTY_SQL, "```sql\nSELECT nope FROM dw.v_match\n```"])
    out = Assistant(llm, dsn).ask("hmm")
    assert out["sql"] is not None and out["rows"] == [] and "no rows" in out["answer"]
    assert [a["error"] for a in out["attempts"]][0] == "the query returned no rows" and len(out["attempts"]) == 2


@needs_db
def test_a_legitimately_empty_query_costs_exactly_one_extra_call(dsn):
    llm = FakeLLM(["```sql\nSELECT * FROM dw.v_player_match WHERE player_search LIKE '%nobody atall%'\n```"] * 2)
    out = Assistant(llm, dsn).ask("Stats for Nobody Atall")
    assert out["rows"] == [] and len(llm.calls) == 2


@needs_db
def test_model_failure_while_phrasing_falls_back_to_the_template(dsn):
    out = Assistant(FakeLLM([GOOD_SQL, LLMError("boom")]), dsn, phrase_answers=True).ask("How many matches?")
    assert out["rows"] and out["answer"].startswith("n: ") and "could not write a summary" in out["answer"]


@needs_db
def test_history_is_in_the_prompt(dsn):
    llm = FakeLLM([GOOD_SQL])
    hist = [{"question": "first question", "sql": "SELECT 1"}, {"question": "second question", "sql": "SELECT 2"}]
    Assistant(llm, dsn).ask("and now this", hist)
    texts = [m["content"] for m in llm.calls[0]]
    assert any("first question" == t for t in texts) and any("SELECT 2" in t for t in texts)
    assert llm.calls[0][-1] == {"role": "user", "content": "and now this"}


@needs_db
def test_the_start_of_the_prompt_is_identical_across_questions(dsn):
    """Model servers can reuse their cached prompt start only if it does not change between questions."""
    llm = FakeLLM([GOOD_SQL, GOOD_SQL])
    a = Assistant(llm, dsn)
    a.ask("first question"), a.ask("a completely different question")
    first, second = llm.calls
    assert first[:-1] == second[:-1] and first[-1] != second[-1]


@needs_db
def test_llm_error_on_first_call_propagates(dsn):
    with pytest.raises(LLMError):
        Assistant(FakeLLM([LLMError("down")]), dsn).ask("hi")


# ---------------------------------------------------------------- database limits
@needs_db
def test_row_cap(dsn):
    cols, rows, truncated = run_select(dsn, "SELECT generate_series(1, 500) AS n", max_rows=200)
    assert len(rows) == 200 and truncated is True


@needs_db
def test_statement_timeout(dsn):
    import psycopg2
    with pytest.raises(psycopg2.Error) as e:
        run_select(dsn, "SELECT pg_sleep(3)", timeout_ms=200)
    assert "timeout" in str(e.value).lower()


@needs_db
def test_trailing_line_comment_does_not_break_the_wrapper(dsn):
    cols, rows, _ = run_select(dsn, "SELECT 1 AS one -- note")
    assert rows == [[1]]


@needs_db
def test_a_second_statement_cannot_be_smuggled_through_the_wrapper(dsn):
    import psycopg2
    with pytest.raises(psycopg2.Error):
        run_select(dsn, "SELECT 1; INSERT INTO dw.dim_team (team_name) VALUES ('x')")
