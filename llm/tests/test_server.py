from conftest import FakeLLM, needs_db
from fastapi.testclient import TestClient

from assistant import Assistant
from llm_client import LLMError
from server import create_app

GOOD_SQL = "```sql\nSELECT COUNT(*) AS n FROM dw.v_match\n```"


@needs_db
def test_ask_returns_the_full_result(dsn):
    client = TestClient(create_app(Assistant(FakeLLM([GOOD_SQL, "Done."]), dsn, phrase_answers=True)))
    r = client.post("/ask", json={"question": "How many matches?", "history": [{"question": "a", "sql": "SELECT 1"}]})
    assert r.status_code == 200
    body = r.json()
    assert body["answer"] == "Done." and body["columns"] == ["n"] and body["attempts"] == []


@needs_db
def test_validation_errors(dsn):
    client = TestClient(create_app(Assistant(FakeLLM([]), dsn)))
    assert client.post("/ask", json={"question": ""}).status_code == 422
    assert client.post("/ask", json={"question": "x" * 501}).status_code == 422
    assert client.post("/ask", json={}).status_code == 422


@needs_db
def test_model_down_is_a_503_with_a_useful_message(dsn):
    client = TestClient(create_app(Assistant(FakeLLM([LLMError("Cannot reach the language model server")]), dsn)))
    r = client.post("/ask", json={"question": "hi"})
    assert r.status_code == 503 and "Cannot reach" in r.json()["detail"]


@needs_db
def test_health(dsn):
    client = TestClient(create_app(Assistant(FakeLLM([]), dsn)))
    assert client.get("/health").json() == {"database": "ok", "views": "ok", "llm": "ok", "model": "fake"}


def test_health_reports_a_broken_database_without_crashing():
    client = TestClient(create_app(Assistant(FakeLLM([]), "postgresql://nobody:x@127.0.0.1:1/none")))
    body = client.get("/health").json()
    assert body["database"].startswith("error")
