"""Every worked example in the prompt must pass the guard and run on the real warehouse."""
import pytest
from conftest import needs_db

from db import run_select
from guard import validate
from schema_context import EXAMPLES


@needs_db
@pytest.mark.parametrize("question,sql", EXAMPLES, ids=[q[:40] for q, _ in EXAMPLES])
def test_example_runs(dsn, question, sql):
    cols, rows, _ = run_select(dsn, validate(sql))
    assert cols and rows
