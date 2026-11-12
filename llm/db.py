"""Run a validated SELECT safely and return JSON-friendly results."""
import datetime
import decimal

import psycopg2

MAX_ROWS = 200
STATEMENT_TIMEOUT_MS = 8000


def _plain(value):
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    return value


def run_select(dsn, sql, max_rows=MAX_ROWS, timeout_ms=STATEMENT_TIMEOUT_MS):
    """-> (columns, rows, truncated). Runs read-only, with a timeout and a hard row cap."""
    conn = psycopg2.connect(dsn)
    try:
        conn.set_session(readonly=True, autocommit=False)
        with conn.cursor() as cur:
            cur.execute(f"SET LOCAL statement_timeout = {int(timeout_ms)}")
            # trailing newline: a final '-- comment' in the model's SQL must not swallow the closing parenthesis
            cur.execute(f"SELECT * FROM (\n{sql}\n) AS q LIMIT {int(max_rows) + 1}")
            columns = [d[0] for d in cur.description]
            fetched = cur.fetchall()
        conn.rollback()
    finally:
        conn.close()
    truncated = len(fetched) > max_rows
    rows = [[_plain(v) for v in row] for row in fetched[:max_rows]]
    return columns, rows, truncated


def db_error_message(exc):
    """First line of a psycopg2 error, e.g. 'column p.name does not exist'."""
    text = (getattr(exc, "pgerror", None) or str(exc)).strip()
    return text.splitlines()[0].replace("ERROR:", "").strip() if text else exc.__class__.__name__
