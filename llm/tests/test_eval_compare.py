import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "eval"))
from run_eval import compare, select_questions  # noqa: E402


def test_exact_and_extra_columns_and_column_order():
    assert compare([["Messi", 30]], [["Messi", 30]])[0]
    assert compare([["Messi", 30]], [[30, "Messi", 2326]])[0]            # extra column, different order
    assert not compare([["Messi", 30]], [["Messi", 29]])[0]


def test_number_tolerance_and_types():
    assert compare([[1.5]], [[1.51]])[0]
    assert not compare([[1.5]], [[1.6]])[0]
    assert not compare([[5]], [["5"]])[0]                                # a string is not a number
    assert compare([[None]], [[None]])[0]


def test_names_match_by_tokens_and_ignore_accents():
    assert compare([["Lionel Messi"]], [["Lionel Andrés Messi Cuccittini"]])[0]
    assert compare([["Gerard Piqué"]], [["gerard pique"]])[0]
    assert not compare([["Lionel Messi"]], [["Luis Suárez"]])[0]


def test_row_count_must_match():
    assert not compare([["a"], ["b"]], [["a"]])[0]
    assert not compare([["a"]], [["a"], ["b"]])[0]


def test_order_only_matters_when_asked():
    ref, got = [["a", 1], ["b", 2]], [["b", 2], ["a", 1]]
    assert compare(ref, got, ordered=False)[0]
    ok, reason = compare(ref, got, ordered=True)
    assert not ok and "row 1" in reason


def test_a_value_cannot_be_matched_twice():
    assert not compare([[7, 7]], [[7]])[0]


def test_select_questions_by_id_and_level():
    golden = [{"id": "a", "level": "easy"}, {"id": "b", "level": "hard"}, {"id": "c", "level": "easy"}]
    assert [g["id"] for g in select_questions(golden)] == ["a", "b", "c"]
    assert [g["id"] for g in select_questions(golden, levels=["easy"])] == ["a", "c"]
    assert [g["id"] for g in select_questions(golden, ids=["b", "c"])] == ["b", "c"]
    assert [g["id"] for g in select_questions(golden, ids=["a", "b"], levels=["easy"])] == ["a"]
