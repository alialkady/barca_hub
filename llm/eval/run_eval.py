#!/usr/bin/env python3
"""
Measure how good the assistant is.

Each question in golden.json has a reference SQL query written by hand. The runner asks the assistant,
runs both queries on the warehouse and compares the RESULTS (not the SQL text).

  docker compose --profile assistant run --rm assistant python eval/run_eval.py
  ... python eval/run_eval.py --levels easy,medium             # the cheaper questions first
  ... python eval/run_eval.py --ids top_scorer,home_record     # a few questions
  Ctrl+C stops early and still prints (and saves) the results so far.
  ... python eval/run_eval.py --dry-run                        # no model: replays the hand-written view queries (checks the harness AND the views)

How results are compared
  * same number of rows; columns may differ in name or order, and the model may return EXTRA columns
  * every reference value must appear in the matching row; numbers match within 0.02
  * names match when one is contained in the other after removing accents (token-wise),
    so 'Lionel Messi' matches 'Lionel Andres Messi Cuccittini'
  * rows must be in the same order only for questions marked "ordered"
Question kinds: "result" (compare data), "refusal" (must decline: out of scope), "blocked" (hostile: nothing may run)
"""
import argparse
import json
import os
import re
import sys
import time
import unicodedata

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from assistant import Assistant, estimate_tokens  # noqa: E402
from db import run_select  # noqa: E402
from guard import validate  # noqa: E402
from llm_client import LLMError, OllamaClient  # noqa: E402
from schema_context import build_messages  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DSN = "postgresql://barca:barca_dev_pw@localhost:5432/barca_dw"


# ------------------------------------------------------------------ comparison
def _tokens(s):
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode().lower()
    return set(re.findall(r"[a-z0-9]+", s))


def _value_match(ref, got):
    if ref is None or got is None:
        return ref is None and got is None
    if isinstance(ref, (int, float)) and not isinstance(ref, bool):
        return isinstance(got, (int, float)) and not isinstance(got, bool) and abs(float(ref) - float(got)) <= 0.02
    if isinstance(got, (int, float)):
        return False
    a, b = _tokens(ref), _tokens(got)
    return bool(a) and bool(b) and (a <= b or b <= a)


def _row_match(ref_row, got_row):
    unused = list(got_row)
    for rv in ref_row:
        hit = next((i for i, gv in enumerate(unused) if _value_match(rv, gv)), None)
        if hit is None:
            return False
        unused.pop(hit)
    return True


def compare(ref_rows, got_rows, ordered=False):
    """-> (ok, reason)"""
    if len(ref_rows) != len(got_rows):
        return False, f"expected {len(ref_rows)} row(s), got {len(got_rows)}"
    if ordered:
        for i, (r, g) in enumerate(zip(ref_rows, got_rows), 1):
            if not _row_match(r, g):
                return False, f"row {i} differs: expected {r}, got {g}"
        return True, ""
    remaining = list(got_rows)
    for r in ref_rows:
        hit = next((i for i, g in enumerate(remaining) if _row_match(r, g)), None)
        if hit is None:
            return False, f"no row matches expected {r}; got {got_rows[:3]}"
        remaining.pop(hit)
    return True, ""


# ------------------------------------------------------------------ dry run: the 'model' replays the reference SQL
class ReferenceLLM:
    """Replays the hand-written VIEW queries (sql_views); they are judged against the base-table reference (sql)."""
    model = "dry-run (reference SQL)"

    def __init__(self, golden):
        self.by_question = {g["question"]: g for g in golden}

    def chat(self, messages):
        if messages[0]["content"].startswith("You turn a SQL result"):
            return "(dry run) answer"
        for m in reversed(messages):
            g = self.by_question.get(m["content"]) if m["role"] == "user" else None
            if g:
                if g["expect"] == "refusal":
                    return "CANNOT_ANSWER: out of scope"
                if g["expect"] == "blocked":
                    return "```sql\nSELECT * FROM raw.statsbomb_events\n```"
                return f"```sql\n{g.get('sql_views') or g['sql']}\n```"
        raise RuntimeError("dry run: question not found")


# ------------------------------------------------------------------ runner
def judge(g, out, dsn):
    expect = g["expect"]
    if expect == "refusal":
        ok = out["sql"] is None and out["answer"].startswith("I can't answer")
        return ok, "" if ok else "should have declined, but did not"
    if expect == "blocked":
        ok = out["sql"] is None
        return ok, "" if ok else f"a query ran: {out['sql'][:80]!r}"
    if out["sql"] is None:
        return False, "no working query: " + (out["attempts"][-1]["error"] if out["attempts"] else out["answer"])
    _, ref_rows, _ = run_select(dsn, validate(g["sql"]))
    return compare(ref_rows, out["rows"], g.get("ordered", False))


def select_questions(golden, ids=None, levels=None):
    out = golden
    if ids:
        out = [g for g in out if g["id"] in set(ids)]
    if levels:
        out = [g for g in out if g["level"] in set(levels)]
    return out


def summarize(results, out_path, model):
    print("\nBy level:")
    for level in ("easy", "medium", "hard", "refusal", "safety"):
        rs = [r for r in results if r["level"] == level]
        if rs:
            print(f"  {level:<8} {sum(r['ok'] for r in rs)}/{len(rs)}")
    passed = sum(r["ok"] for r in results)
    n = max(1, len(results))
    print(f"\nOverall: {passed}/{len(results)} = {100 * passed / n:.0f}%   "
          f"(avg {sum(r['seconds'] for r in results) / n:.1f}s per question)")
    json.dump({"model": model, "results": results}, open(out_path, "w"), indent=1)
    print(f"Details written to {out_path}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--ids", help="comma-separated question ids")
    ap.add_argument("--levels", help="comma-separated: easy,medium,hard,refusal,safety")
    ap.add_argument("--dsn", default=os.environ.get("DATABASE_URL", DEFAULT_DSN))
    ap.add_argument("--ollama-url", default=os.environ.get("OLLAMA_URL", "http://localhost:11434"))
    ap.add_argument("--model", default=os.environ.get("OLLAMA_MODEL", "qwen2.5-coder:7b"))
    ap.add_argument("--out", default=os.path.join(HERE, "last_run.json"))
    args = ap.parse_args()

    golden = select_questions(
        json.load(open(os.path.join(HERE, "golden.json"))),
        args.ids.split(",") if args.ids else None,
        args.levels.split(",") if args.levels else None,
    )
    thread_env = os.environ.get("OLLAMA_NUM_THREAD")
    llm = ReferenceLLM(golden) if args.dry_run else OllamaClient(
        args.ollama_url, args.model, num_ctx=int(os.environ.get("OLLAMA_NUM_CTX", "8192")),
        num_thread=int(thread_env) if thread_env else None)
    assistant = Assistant(llm, args.dsn)

    prompt_tokens = estimate_tokens(build_messages("question", assistant.coverage()))
    print(f"Model: {llm.model}   Questions: {len(golden)}   Prompt: ~{prompt_tokens} tokens "
          f"(only the first question pays for it in full if the model server caches the prompt start)\n")
    results = []
    try:
        for g in golden:
            t0 = time.time()
            try:
                out = assistant.ask(g["question"], phrase=False)     # the evaluation only needs the rows
                ok, reason = judge(g, out, args.dsn)
            except LLMError as e:
                print(f"\nStopped: {e}")
                break
            secs = time.time() - t0
            results.append({"id": g["id"], "level": g["level"], "ok": ok, "reason": reason, "seconds": round(secs, 1),
                            "attempts": len(out["attempts"]) + (1 if out["sql"] else 0), "sql": out["sql"],
                            "answer": out["answer"]})
            print(f"{'PASS' if ok else 'FAIL'}  {g['level']:<8} {g['id']:<24} {secs:5.1f}s"
                  + ("" if ok else f"   <- {reason}"), flush=True)
            json.dump({"model": llm.model, "results": results}, open(args.out, "w"), indent=1)   # keep progress
    except KeyboardInterrupt:
        print("\nInterrupted: showing the results so far.")
    if results:
        summarize(results, args.out, llm.model)


if __name__ == "__main__":
    main()
