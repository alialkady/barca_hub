"""
SQL guard: the model writes SQL, this module decides whether it may run.

Layers of protection (the guard is only the first one):
  1. guard.validate()   parses the SQL and allows ONE read-only statement that touches only the
                        whitelisted dw.* tables and no dangerous functions
  2. db.run_select()    runs it in a READ ONLY transaction with a statement timeout and a row cap
  3. (recommended)      connect with the read-only role from warehouse/models/assistant_readonly_role.sql
"""
import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

ALLOWED_SCHEMA = "dw"
ALLOWED_TABLES = {
    # what the model is told about (semantic views, see warehouse/models/semantic_views.sql)
    "v_player_match", "v_shot", "v_match",
    # base tables: not in the prompt, but allowed so hand-written reference queries can use them
    "dim_player", "dim_team", "dim_season", "dim_match",
    "fact_player_match_stats", "fact_shot",
}
FORBIDDEN_FUNC_PREFIXES = ("pg_", "lo_", "dblink", "query_to_", "table_to_xml", "database_to_xml")
FORBIDDEN_FUNCS = {"set_config", "current_setting", "txid_current", "inet_server_addr", "inet_client_addr"}
_FORBIDDEN_NODES = tuple(
    getattr(exp, name) for name in
    ("Insert", "Update", "Delete", "Drop", "Create", "Alter", "Command", "Merge", "Into", "Lock",
     "Copy", "TruncateTable", "Grant", "Set", "Use", "Transaction", "Commit", "Rollback")
    if hasattr(exp, name)
)
_ALLOWED_ROOTS = tuple(getattr(exp, n) for n in ("Select", "Union", "SetOperation") if hasattr(exp, n))


class UnsafeSQL(ValueError):
    """The query is not allowed to run. The message is safe to show to the model and the user."""


def validate(sql):
    """Return the cleaned SQL (no trailing semicolon) or raise UnsafeSQL."""
    sql = (sql or "").strip().rstrip(";").strip()
    if not sql:
        raise UnsafeSQL("The query is empty.")
    try:
        statements = [s for s in sqlglot.parse(sql, read="postgres") if s is not None]
    except SqlglotError as e:
        raise UnsafeSQL(f"The SQL could not be parsed: {str(e).splitlines()[0]}")
    if len(statements) != 1:
        raise UnsafeSQL("Exactly one statement is allowed.")
    tree = statements[0]

    if not isinstance(tree, _ALLOWED_ROOTS):
        raise UnsafeSQL("Only SELECT queries (optionally with WITH) are allowed.")
    for node in tree.walk():
        node = node[0] if isinstance(node, tuple) else node
        if isinstance(node, _FORBIDDEN_NODES):
            raise UnsafeSQL(f"{type(node).__name__.upper()} is not allowed; the query must be a plain read-only SELECT.")

    for f in tree.find_all(exp.Func):
        name = (f.name if isinstance(f, exp.Anonymous) else f.sql_name()).lower()
        if name in FORBIDDEN_FUNCS or name.startswith(FORBIDDEN_FUNC_PREFIXES):
            raise UnsafeSQL(f"The function {name}() is not allowed.")

    cte_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    for table in tree.find_all(exp.Table):
        if isinstance(table.this, exp.Func):          # e.g. generate_series(...) - function rules above apply
            continue
        name, schema = table.name.lower(), (table.db or "").lower()
        if not schema and name in cte_names:
            continue
        if table.catalog or schema != ALLOWED_SCHEMA or name not in ALLOWED_TABLES:
            shown = f"{schema}.{name}" if schema else name
            raise UnsafeSQL(
                f"Table '{shown}' is not available. Use only: "
                + ", ".join(f"{ALLOWED_SCHEMA}.{t}" for t in sorted(ALLOWED_TABLES)) + "."
            )
    return sql
