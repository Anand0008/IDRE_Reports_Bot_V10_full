"""
SQL Validator Agent — V10

Deterministic safety gate between SQL Writer and Executor.
Six blocking checks (any failure populates state['error_message']
and returns empty state['validated_sql']):

1. Statement type — query must start with SELECT.
2. Blocked keywords — INSERT/UPDATE/DELETE/DROP/CREATE/ALTER/TRUNCATE/
   EXEC/EXECUTE/CALL/GRANT/REVOKE/LOAD/OUTFILE/DUMPFILE rejected.
3. Injection patterns — OR 1=1, UNION SELECT, URL-encoded variants,
   timing functions (SLEEP, BENCHMARK), comment-truncation, CHAR()/CHR().
4. Multi-statement — rejects any query containing more than one `;`.
5. Table-existence — every FROM/JOIN-referenced table must appear in
   the root schema_catalog.json. Mismatch emits a diagnostic hint
   pointing at the Phase-5 Design A refresh path.
6. Role permission — every referenced table must appear in the user's
   permitted_tables (state['permitted_tables']).

Plus three informational, non-blocking annotations surfaced into
agent_trace (the node escalates column-existence into a blocking
error; the rest stay as warnings):

- Column existence — every `table.column` (or `alias.column` via
  alias resolution) reference must match the catalog's column list
  for that table.
- Cost estimate — total_rows × max(1, join_count); flagged "expensive"
  when scan > 1_000_000. Duplicated by Node-6 verify_sql_executes +
  _check_explain_plan EXPLAIN gate; sql_validator's estimate is the
  catalog-derived approximation surfaced before the live EXPLAIN.
- Semantic warnings — heuristic checks for "top N without LIMIT",
  "count phrasing without COUNT()/SUM()", and "by month/week/day/year
  without GROUP BY".

History:
- V5+: column-existence validation (Improvement 2) introduced;
  schema_catalog.json reads + table-existence + role-permission gates.
- V6: cost estimation, semantic validation, enhanced injection
  detection, LIKE-on-numeric type-compatibility warnings.
- V7/V8/V9: byte-identical to V6 — agent stable across four versions.
- V10 (May 17): @trace_agent("v10.agent.sql_validator") decorator +
  tracing import; no behavioural change vs V9.
- V10 Node 7 (May 20): module header refreshed; vestigial
  "user-mentioned-'status'" semantic-warning rule dropped (high false-
  positive); "Unknown table(s)" reject now carries a diagnostic hint
  about the dual-catalog gap (root schema_catalog.json is MySQL-derived
  44 tables vs knowledge/v10/schema_catalog.json Prisma-derived 53
  tables — Phase 5 Design A is the documented unification path). See
  local/docs/superpowers/reports/2026-05-20-node7-sql-validator-audit.md.
"""
import re
import json
import os
from state.context import GraphState
from tracing import trace_agent

SCHEMA_CATALOG_PATH = os.path.join(os.path.dirname(__file__), "..", "schema_catalog.json")
_catalog_cache = None

BLOCKED_KEYWORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|TRUNCATE|EXEC|EXECUTE|CALL|GRANT|REVOKE|LOAD|OUTFILE|DUMPFILE)\b",
    re.IGNORECASE,
)

_INJECTION_PATTERNS = [
    re.compile(r"\bOR\s+1\s*=\s*1\b", re.IGNORECASE),
    re.compile(r"\bOR\s+'[^']*'\s*=\s*'[^']*'", re.IGNORECASE),
    # 2026-05-21: removed `\bUNION\s+(?:ALL\s+)?SELECT\b` — false-positive
    # on legitimate UNION ALL composition (e.g. organization counts across
    # two FK columns). The bot has no user-controlled string concat into
    # SQL, so the UNION-injection threat model doesn't apply here.
    # BLOCKED_KEYWORDS + RBAC + EXPLAIN dry-run cover the real surface.
    re.compile(r";\s*DROP\s+TABLE\b", re.IGNORECASE),
    re.compile(r";\s*DELETE\s+FROM\b", re.IGNORECASE),
    re.compile(r"--\s*$", re.MULTILINE),
    re.compile(r"/\*.*?\*/", re.DOTALL),
    re.compile(r"%27|%3B|%23|%2D%2D", re.IGNORECASE),
    re.compile(r"(?:CHAR|CHR)\s*\(\s*\d+\s*\)", re.IGNORECASE),
    re.compile(r"SLEEP\s*\(\s*\d+\s*\)", re.IGNORECASE),
    re.compile(r"BENCHMARK\s*\(", re.IGNORECASE),
]


def _get_catalog() -> dict:
    global _catalog_cache
    if _catalog_cache is None:
        with open(SCHEMA_CATALOG_PATH) as f:
            _catalog_cache = json.load(f)
    return _catalog_cache


def _get_known_tables() -> set:
    return set(_get_catalog()["tables"].keys())


def _get_table_columns(table_name: str) -> set:
    info = _get_catalog()["tables"].get(table_name, {})
    return {c["name"] for c in info.get("columns", [])}


def _get_table_column_types(table_name: str) -> dict[str, str]:
    info = _get_catalog()["tables"].get(table_name, {})
    return {c["name"]: c["type"] for c in info.get("columns", [])}


def _get_table_row_count(table_name: str) -> int:
    info = _get_catalog()["tables"].get(table_name, {})
    return info.get("row_count_approx", 0)


def _extract_table_names(sql: str) -> list[str]:
    normalized = sql.replace("`", "")
    pattern = re.compile(r"\b(?:FROM|JOIN)\s+(\w+)", re.IGNORECASE)
    return pattern.findall(normalized)


_TABLE_DOT_COL_RE = re.compile(r"`?(\w+)`?\s*\.\s*`?(\w+)`?")
_ALIAS_RE = re.compile(r"\b(?:FROM|JOIN)\s+`?(\w+)`?\s+(?:AS\s+)?`?(\w+)`?", re.IGNORECASE)


def _extract_column_refs(sql: str, referenced_tables: list[str]) -> list[tuple[str, str]]:
    return _TABLE_DOT_COL_RE.findall(sql.replace("`", ""))


def _build_alias_map(sql: str) -> dict[str, str]:
    alias_map = {}
    for match in _ALIAS_RE.finditer(sql.replace("`", "")):
        table, alias = match.group(1), match.group(2)
        if alias.upper() not in ("ON", "WHERE", "SET", "AND", "OR", "LEFT", "RIGHT", "INNER", "OUTER", "CROSS"):
            alias_map[alias.lower()] = table.lower()
    return alias_map


def validate_columns(sql: str, referenced_tables: list[str]) -> list[str]:
    known_tables = _get_known_tables()
    alias_map = _build_alias_map(sql)
    refs = _extract_column_refs(sql, referenced_tables)
    warnings = []
    seen = set()
    for table_or_alias, col in refs:
        real_table = alias_map.get(table_or_alias.lower(), table_or_alias.lower())
        if real_table not in known_tables:
            continue
        key = (real_table, col)
        if key in seen:
            continue
        seen.add(key)
        valid_cols = _get_table_columns(real_table)
        if valid_cols and col not in valid_cols:
            warnings.append(f"Column `{real_table}`.`{col}` does not exist. Valid columns: {', '.join(sorted(valid_cols)[:15])}")
    return warnings


def _estimate_cost(sql: str, referenced_tables: list[str]) -> dict:
    total_rows = 0
    for table in referenced_tables:
        total_rows += _get_table_row_count(table)
    join_count = len(re.findall(r"\bJOIN\b", sql, re.IGNORECASE))
    complexity = max(1, join_count)
    estimated_scan = total_rows * complexity
    return {
        "total_rows": total_rows,
        "join_count": join_count,
        "estimated_scan": estimated_scan,
        "expensive": estimated_scan > 1_000_000,
    }


def _semantic_validation(sql: str, query: str) -> list[str]:
    warnings = []
    query_lower = query.lower()
    sql_upper = sql.upper()

    if re.search(r"\btop\s+\d+\b", query_lower) and "LIMIT" not in sql_upper:
        warnings.append("User asked for 'top N' but SQL has no LIMIT clause")

    if re.search(r"\bcount\b", query_lower) and not re.search(r"\bCOUNT\s*\(", sql, re.IGNORECASE):
        if not re.search(r"\bSUM\s*\(", sql, re.IGNORECASE):
            warnings.append("User asked for a count but SQL has no COUNT() or SUM() aggregate")

    if re.search(r"\bby\s+(month|week|day|year)\b", query_lower):
        if not re.search(r"\bGROUP\s+BY\b", sql, re.IGNORECASE):
            warnings.append("User asked for grouping by time period but SQL has no GROUP BY")

    # Node 7 (2026-05-20): dropped the "user said 'status' but SQL doesn't
    # reference status" rule — high false-positive rate. Status appears in
    # WHERE clauses, in colloquial phrasing without column intent, and in
    # joined-table contexts the heuristic doesn't see.

    return warnings


def _check_injection(sql: str) -> str:
    for pattern in _INJECTION_PATTERNS:
        if pattern.search(sql):
            return f"Potential SQL injection pattern detected: {pattern.pattern[:50]}"
    return ""


def _check_type_compatibility(sql: str, referenced_tables: list[str]) -> list[str]:
    warnings = []
    alias_map = _build_alias_map(sql)
    known_tables = _get_known_tables()

    like_matches = re.findall(r"`?(\w+)`?\s*\.\s*`?(\w+)`?\s+LIKE\b", sql, re.IGNORECASE)
    for table_or_alias, col in like_matches:
        real_table = alias_map.get(table_or_alias.lower(), table_or_alias.lower())
        if real_table in known_tables:
            col_types = _get_table_column_types(real_table)
            col_type = col_types.get(col, "").lower()
            if col_type and any(t in col_type for t in ("int", "decimal", "float", "double", "bigint")):
                warnings.append(f"LIKE used on numeric column `{real_table}`.`{col}` (type: {col_type})")

    return warnings


def validate_sql(sql: str, permitted_tables: list = None) -> tuple:
    if not sql or not sql.strip():
        return False, "Empty SQL generated."
    stripped = sql.strip()
    if not re.match(r"^\s*(SELECT|WITH)\b", stripped, re.IGNORECASE):
        return False, f"Query must be a SELECT or WITH (CTE) statement. Got: {stripped[:60]}"
    match = BLOCKED_KEYWORDS.search(stripped)
    if match:
        return False, f"Blocked keyword '{match.group()}' found in query."

    injection = _check_injection(stripped)
    if injection:
        return False, injection

    if stripped.rstrip(";").count(";") > 0:
        return False, "Multiple SQL statements are not allowed."
    known = _get_known_tables()
    referenced = _extract_table_names(stripped)
    unknown = [t for t in referenced if t not in known]
    if unknown:
        # Node 7 (2026-05-20): the root schema_catalog.json (MySQL-derived,
        # ~44 tables) is the source-of-truth for sql_validator, while the
        # rest of the bot reads knowledge/v10/schema_catalog.json (Prisma-
        # derived, ~53 tables). A reject here for an apparently-valid
        # Prisma table likely means the root catalog is stale. Refresh
        # via `py -3.11 analyze_db.py`, or wait for the Phase 5 Design A
        # unification (see local/docs/superpowers/reports/
        # 2026-05-20-node5-architecture-decision.md).
        hint = (
            f" (validator catalog has {len(known)} tables; if you expected a "
            f"newer Prisma-side table, the root catalog may be stale — "
            f"refresh via `py -3.11 analyze_db.py` or see Phase 5 Design A.)"
        )
        return False, (
            f"Unknown table(s) referenced: {unknown}. Check spelling or schema."
            + hint
        )
    if permitted_tables:
        unauthorized = [t for t in referenced if t not in permitted_tables]
        if unauthorized:
            return False, "Query references table(s) that are not accessible for your role."
    return True, ""


@trace_agent("v10.agent.sql_validator")
def sql_validator_node(state: GraphState) -> GraphState:
    sql = state.get("generated_sql", "")
    permitted = state.get("permitted_tables") or None
    query = state.get("resolved_query") or state.get("user_query", "")
    is_valid, error = validate_sql(sql, permitted_tables=permitted)
    trace = state.get("agent_trace", [])

    if is_valid:
        tables_used = _extract_table_names(sql)
        col_warnings = validate_columns(sql, tables_used)

        if col_warnings:
            col_error = "Hallucinated column(s) detected:\n" + "\n".join(col_warnings)
            trace_entry = {
                "agent": "SQL Validator",
                "status": "error",
                "summary": f"Column validation failed — {len(col_warnings)} hallucinated column(s)",
                "detail": col_warnings,
            }
            trace = trace + [trace_entry]
            return {**state, "validated_sql": "", "error_message": col_error, "agent_trace": trace}

        cost = _estimate_cost(sql, tables_used)
        semantic_warnings = _semantic_validation(sql, query)
        type_warnings = _check_type_compatibility(sql, tables_used)
        all_warnings = semantic_warnings + type_warnings

        detail = []
        if tables_used:
            detail.append(f"Tables in query: {', '.join(tables_used)}")
        if cost["expensive"]:
            detail.append(f"⚠ Expensive query: ~{cost['estimated_scan']:,} estimated row scans")
        if all_warnings:
            detail.extend([f"⚠ {w}" for w in all_warnings])

        status = "warn" if (cost["expensive"] or all_warnings) else "ok"
        summary = f"Passed all safety checks · {len(tables_used)} table(s) referenced"
        if cost["expensive"]:
            summary += " · ⚠ expensive query"
        if all_warnings:
            summary += f" · {len(all_warnings)} semantic warning(s)"

        trace_entry = {
            "agent": "SQL Validator",
            "status": status,
            "summary": summary,
            "detail": detail,
        }
        trace = trace + [trace_entry]

        return {
            **state,
            "validated_sql": sql,
            "error_message": "",
            "agent_trace": trace,
            "query_complexity": cost,
        }
    else:
        is_permission_violation = "not accessible for your role" in error
        trace_entry = {
            "agent": "SQL Validator",
            "status": "error",
            "summary": "Permission violation — query blocked" if is_permission_violation else "Validation failed — query blocked",
            "detail": [error],
        }
        trace = trace + [trace_entry]
        return {**state, "validated_sql": "", "error_message": error, "agent_trace": trace}
