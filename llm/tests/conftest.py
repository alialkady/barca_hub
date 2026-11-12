import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DSN = os.environ.get("DATABASE_URL", "postgresql://barca:barca_dev_pw@localhost:5432/barca_dw")


def _db_up():
    try:
        import psycopg2
        psycopg2.connect(DSN, connect_timeout=2).close()
        return True
    except Exception:
        return False


needs_db = pytest.mark.skipif(not _db_up(), reason="warehouse database not reachable (set DATABASE_URL)")


class FakeLLM:
    """Stands in for the model: returns scripted replies in order and records every prompt it was sent."""
    model = "fake"

    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def chat(self, messages):
        self.calls.append(messages)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def health(self):
        return "ok"


@pytest.fixture
def dsn():
    return DSN
