"""
The assistant pipeline:  question -> LLM writes SQL -> guard -> run read-only -> (retry once on error)
                         -> LLM phrases the answer from the rows.
The model never sees more than the rows returned, and never touches the database itself.
"""
import logging
import re
import time

import psycopg2

from db import MAX_ROWS, db_error_message, run_select
from guard import UnsafeSQL, validate
from llm_client import LLMError
from schema_context import ANSWER_PROMPT, SCHEMA_REMINDER, build_messages

log = logging.getLogger("assistant")

_FENCED = re.compile(r"```(?:sql|postgresql|postgres)?[ \t]*\n?(.*?)```", re.S | re.I)
_UNCLOSED = re.compile(r"```(?:sql|postgresql|postgres)?[ \t]*\n?(.*)", re.S | re.I)
_REFUSAL = re.compile(r"^\s*CANNOT_ANSWER:\s*(.*)", re.S | re.I)
SHOW_ROWS_TO_MODEL = 30


def extract_sql(text):
    """Model reply -> (sql, refusal_reason). At most one of them is not None."""
    text = (text or "").strip()
    m = _FENCED.search(text) or _UNCLOSED.search(text)
    candidate = m.group(1).strip() if m else text
    refusal = _REFUSAL.match(candidate) or _REFUSAL.match(text)
    if refusal:
        return None, (refusal.group(1).strip().splitlines() or ["no reason given"])[0]
    if m:
        return candidate, None
    if re.match(r"(?is)^\s*(select|with)\b", text):
        return text, None
    return None, None


def markdown_table(columns, rows):
    def cell(v):
        return "" if v is None else str(v)
    lines = ["| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
    lines += ["| " + " | ".join(cell(v) for v in row) + " |" for row in rows]
    return "\n".join(lines)


def template_answer(columns, rows, truncated=False):
    """Plain one-line answer built from the rows, with no model call (fast, and cannot invent facts)."""
    def fmt(row):
        return ", ".join(f"{c.replace('_', ' ')}: {v}" for c, v in zip(columns, row))
    if len(rows) == 1:
        return fmt(rows[0])
    more = " (there are more; the first 200 are kept)" if truncated else ""
    return f"{len(rows)} rows{more}. First row: {fmt(rows[0])}"


def estimate_tokens(messages):
    return sum(len(m["content"]) for m in messages) // 3


class Assistant:
    def __init__(self, llm, dsn, max_attempts=2, coverage_ttl_s=600, phrase_answers=False):
        # phrase_answers=True makes a SECOND model call per question to write a sentence. It is slow on a CPU
        # (and the call has a different prompt start, which defeats the model server's prompt cache).
        self.llm, self.dsn, self.max_attempts, self.phrase_answers = llm, dsn, max_attempts, phrase_answers
        self._coverage, self._coverage_at, self._coverage_ttl = None, 0.0, coverage_ttl_s

    # ------------------------------------------------------------ coverage text (what is in the warehouse)
    def coverage(self):
        if self._coverage is None or time.time() - self._coverage_at > self._coverage_ttl:
            try:
                _, rows, _ = run_select(self.dsn, """
                    SELECT COUNT(*), MIN(m.match_date), MAX(m.match_date),
                           MIN(s.season_label), MAX(s.season_label), STRING_AGG(DISTINCT s.competition, ', ')
                    FROM dw.dim_match m JOIN dw.dim_season s ON s.season_key = m.season_key""")
                n, first, last, s_first, s_last, comps = rows[0]
                self._coverage = (
                    f"{n} matches from {first} to {last}; competition(s): {comps}; seasons {s_first} to {s_last}. "
                    f"Every match involves Barcelona. Player stats exist only for those matches (opponents included). "
                    f"The data comes from StatsBomb's free open data, so it stops at the latest season listed and some matches are missing."
                ) if n else "The warehouse is empty."
            except psycopg2.Error:
                self._coverage = "Unknown (could not read the warehouse)."
            self._coverage_at = time.time()
        return self._coverage

    # ------------------------------------------------------------ main entry point
    def ask(self, question, history=None, phrase=None):
        phrase = self.phrase_answers if phrase is None else phrase
        question = (question or "").strip()
        messages = build_messages(question, self.coverage(), history)
        attempts = []
        empty = None            # a query that ran fine but returned no rows (kept in case the retry goes wrong)
        for i in range(self.max_attempts):
            raw = self.llm.chat(messages)
            sql, refusal = extract_sql(raw)
            if refusal:
                return self._result(f"I can't answer that from the data I have: {refusal}", None, attempts=attempts)
            error = None
            if sql is None:
                error = "The reply did not contain a SQL query."
            else:
                try:
                    clean = validate(sql)
                    columns, rows, truncated = run_select(self.dsn, clean)
                except UnsafeSQL as e:
                    error = str(e)
                except psycopg2.Error as e:
                    error = db_error_message(e)
                else:
                    if rows or i == self.max_attempts - 1:
                        answer = self._phrase(question, columns, rows, truncated, phrase)
                        return self._result(answer, clean, columns, rows, truncated, attempts)
                    # No rows on the first try: very often a wrong literal (a position, a name, a season format).
                    # Give the model ONE chance to check its filters; if it repeats the query, the empty answer stands.
                    empty = (clean, columns, rows, truncated)
                    attempts.append({"sql": clean, "error": "the query returned no rows"})
                    messages += [
                        {"role": "assistant", "content": raw},
                        {"role": "user", "content": "That query ran but returned no rows, so a filter value is probably wrong. "
                                                    "These are the exact views, columns and values:\n" + SCHEMA_REMINDER +
                                                    "\nFix the query, or if you are sure nothing matches, return the same query again."},
                    ]
                    continue
            log.info("attempt failed: %s | sql=%r", error, sql)
            attempts.append({"sql": sql, "error": error})
            hint = ""
            if "not available" in error or "does not exist" in error:
                hint = "\nThese are ALL the views, columns and values that exist:\n" + SCHEMA_REMINDER + "\n"
            messages += [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": f"That did not work: {error}{hint}\n"
                                            "Return only the corrected SQL in a ```sql block, or CANNOT_ANSWER: <reason>."},
            ]
        if empty:                                           # the retry failed, but the first query did run
            clean, columns, rows, truncated = empty
            return self._result(self._phrase(question, columns, rows, truncated, phrase), clean, columns, rows,
                                truncated, attempts)
        return self._result(
            "I couldn't turn that into a working query. Try rephrasing, for example by naming the player, "
            "the season (like 2020/2021) and the stat you want.", None, attempts=attempts)

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _result(answer, sql, columns=None, rows=None, truncated=False, attempts=None):
        return {"answer": answer, "sql": sql, "columns": columns or [], "rows": rows or [],
                "truncated": truncated, "attempts": attempts or []}

    def _phrase(self, question, columns, rows, truncated, phrase=True):
        if not rows:
            return ("The query ran but returned no rows. Check the spelling of names, the season format "
                    "(like 2020/2021) and the team. The warehouse holds " + self.coverage().split(". Every match")[0] + ".")
        if not phrase:
            return template_answer(columns, rows, truncated)
        shown = rows[:SHOW_ROWS_TO_MODEL]
        note = ""
        if truncated or len(rows) > len(shown):
            note = f"\n(Only the first {len(shown)} rows are shown here; there are more.)"
        prompt = f"Question: {question}\n\nSQL result:\n{markdown_table(columns, shown)}{note}"
        try:
            return self.llm.chat([{"role": "system", "content": ANSWER_PROMPT},
                                  {"role": "user", "content": prompt}]).strip()
        except LLMError:
            return template_answer(columns, rows, truncated) + "  (the model could not write a summary)"
