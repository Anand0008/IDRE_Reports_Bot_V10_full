"""
Executor Agent — V10

Substitutes runtime placeholders via `_bind_runtime_params(sql, state)`
(handles `:now` from state.now_anchor_iso and `:current_user_id` from
state.user_identity), then executes the validated SQL. Routes
analytical queries (GROUP BY / ORDER BY) to the read replica if
DB_READ_REPLICA_HOST is set. Materializes the result set in
data/materialized_results/<sha>.json when the same SQL hash has been
seen ≥3 times (TTL 1h).
Production row cap is 100,000 (env V10_ROW_CAP), bypassed entirely
when V10_DISABLE_ROW_CAP=1 — used by the test harness.

Statement timeout: 30s for online queries, 120s for CSV downloads
(execute_unlimited).

History:
- V6: read replica routing for analytical queries; result
  materialization (`data/materialized_results/`); query frequency
  tracking (`data/query_frequency.json`).
- V10: production row cap raised 50K → 100K; V10_DISABLE_ROW_CAP env
  override; `_bind_now()` substitutes `:now` placeholder before
  execution; cache hash computed AFTER `:now` binding so materialized
  results are keyed by the actual executed SQL.
- V10 Node 8 (May 20): renamed `_bind_now` → `_bind_runtime_params`,
  added `:current_user_id` binding from state.user_identity (with a
  conservative ^[A-Za-z0-9_-]+$ safety regex). Smoke-test S4 fix.
"""
import hashlib
import json
import os
import re
import time
from sqlalchemy import text, create_engine
from db.connector import get_engine
from state.context import GraphState
from tracing import trace_agent

# V10: production cap 50k → 100k; tests bypass via V10_DISABLE_ROW_CAP
DEFAULT_ROW_CAP = 100_000
ROW_LIMIT = int(os.environ.get("V10_ROW_CAP", DEFAULT_ROW_CAP))
DISABLE_ROW_CAP = os.environ.get("V10_DISABLE_ROW_CAP", "").lower() in ("1", "true", "yes")
QUERY_TIMEOUT_SECONDS = 30
DOWNLOAD_TIMEOUT_SECONDS = 120
CSV_ROW_LIMIT = 100_000

_DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
_MATERIALIZED_DIR = os.path.join(_DATA_DIR, "materialized_results")
_QUERY_FREQ_PATH = os.path.join(_DATA_DIR, "query_frequency.json")
_MATERIALIZED_TTL = 3600


def _enforce_limit(sql: str) -> str:
    stripped = sql.strip().rstrip(";")
    if DISABLE_ROW_CAP:
        return stripped
    if re.search(r"\bLIMIT\b", stripped, re.IGNORECASE):
        return stripped
    if re.search(r"\bCOUNT\s*\(|SUM\s*\(|AVG\s*\(|MIN\s*\(|MAX\s*\(", stripped, re.IGNORECASE):
        if not re.search(r"\bGROUP\s+BY\b", stripped, re.IGNORECASE):
            return stripped
    return f"{stripped} LIMIT {ROW_LIMIT}"


_USER_IDENTITY_SAFE_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


def _bind_runtime_params(sql: str, state: dict) -> str:
    """Substitute pipeline-known runtime placeholders before execution.

    Two placeholders are wired today:

    - ``:now`` — the temporal anchor. ``find_filter_pattern`` returns SQL
      fragments such as ``DATE_FORMAT(:now, '%Y-%m-01 00:00:00')`` so the
      bot's query is evaluated at the same logical instant the harness/
      test captured. Source: ``state.now_anchor_iso``. Fallback:
      ``UTC_TIMESTAMP()`` if no anchor is in state — keeps queries
      running but breaks temporal-anchoring guarantees.

    - ``:current_user_id`` — emitted by SQL Writer for user-scoped
      queries like "cases assigned to me" (`case.assignedToId = :current_user_id`).
      Source: ``state.user_identity``. Substituted only if it passes a
      conservative safety regex (``^[A-Za-z0-9_\\-]+$``) — if the value
      contains anything else (whitespace, quote, semicolon, etc.) the
      placeholder is left in so SQLAlchemy raises a descriptive
      ``InvalidRequestError`` rather than risking a malformed/unsafe
      substitution. Identity values from session state are normally
      safe; the regex is a defence-in-depth check.

    Node 8 (2026-05-20): renamed from ``_bind_now(sql, now_iso)`` and
    extended to handle ``:current_user_id``. Original ``_bind_now`` was a
    smoke-test gap — see I2 in
    local/docs/superpowers/reports/2026-05-20-node8-executor-debugger-audit.md.
    """
    # :now substitution
    if ":now" in sql:
        now_iso = state.get("now_anchor_iso", "") if isinstance(state, dict) else ""
        if not now_iso:
            sql = sql.replace(":now", "UTC_TIMESTAMP()")
        else:
            # ISO 8601 → MySQL DATETIME literal 'YYYY-MM-DD HH:MM:SS'.
            dt = now_iso.replace("T", " ").split("+")[0].split(".")[0].rstrip("Z").strip()
            sql = sql.replace(":now", f"'{dt}'")

    # :current_user_id substitution (Node 8)
    if ":current_user_id" in sql:
        user_id = (state.get("user_identity", "") if isinstance(state, dict) else "").strip()
        if user_id and _USER_IDENTITY_SAFE_RE.match(user_id):
            sql = sql.replace(":current_user_id", f"'{user_id}'")
        # else: leave the placeholder; SQLAlchemy will raise a clear bind error.

    return sql


def _sql_hash(sql: str) -> str:
    normalized = " ".join(sql.lower().split())
    return hashlib.md5(normalized.encode()).hexdigest()[:12]


def _get_read_replica_engine():
    replica_host = os.environ.get("DB_READ_REPLICA_HOST")
    if not replica_host:
        return None
    try:
        from config.settings import get_settings
        s = get_settings()
        ssl_args = {}
        ssl_path = os.path.abspath(s.db_ssl_ca)
        if os.path.exists(ssl_path):
            ssl_args = {"ssl_ca": ssl_path}
        url = f"mysql+mysqlconnector://<user>:<password>@{replica_host}:{s.db_port}/{s.db_name}"
        return create_engine(url, connect_args=ssl_args, pool_pre_ping=True, pool_recycle=300)
    except Exception:
        return None


def _is_analytical_query(sql: str) -> bool:
    return bool(
        re.search(r"\bGROUP\s+BY\b", sql, re.IGNORECASE) or
        re.search(r"\bORDER\s+BY\b", sql, re.IGNORECASE)
    )


def _track_query_frequency(sql_hash: str) -> int:
    try:
        if os.path.exists(_QUERY_FREQ_PATH):
            with open(_QUERY_FREQ_PATH, encoding="utf-8") as f:
                freq = json.load(f)
        else:
            freq = {}
        freq[sql_hash] = freq.get(sql_hash, 0) + 1
        os.makedirs(os.path.dirname(_QUERY_FREQ_PATH), exist_ok=True)
        with open(_QUERY_FREQ_PATH, "w", encoding="utf-8") as f:
            json.dump(freq, f)
        return freq[sql_hash]
    except OSError:
        return 1


def _check_materialized(sql_hash: str) -> list[dict]:
    path = os.path.join(_MATERIALIZED_DIR, f"{sql_hash}.json")
    if not os.path.exists(path):
        return None
    try:
        mtime = os.path.getmtime(path)
        if time.time() - mtime > _MATERIALIZED_TTL:
            os.remove(path)
            return None
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _save_materialized(sql_hash: str, rows: list[dict]) -> None:
    try:
        os.makedirs(_MATERIALIZED_DIR, exist_ok=True)
        path = os.path.join(_MATERIALIZED_DIR, f"{sql_hash}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rows[:ROW_LIMIT], f, default=str)
    except OSError:
        pass


def execute_query(sql: str) -> tuple[list[dict], str]:
    sql = _enforce_limit(sql)
    engine_to_use = get_engine()

    if _is_analytical_query(sql):
        replica = _get_read_replica_engine()
        if replica:
            engine_to_use = replica

    try:
        with engine_to_use.connect() as conn:
            conn.execute(text(f"SET SESSION MAX_EXECUTION_TIME={QUERY_TIMEOUT_SECONDS * 1000}"))
            result = conn.execute(text(sql))
            columns = list(result.keys())
            rows = [dict(zip(columns, row)) for row in result.fetchall()]
            return rows, ""
    except Exception as e:
        return [], str(e)


def execute_unlimited(sql: str) -> tuple[list[dict], str]:
    stripped = sql.strip().rstrip(";")
    if not re.search(r"\bLIMIT\b", stripped, re.IGNORECASE):
        is_agg = (re.search(r"\bCOUNT\s*\(|SUM\s*\(|AVG\s*\(|MIN\s*\(|MAX\s*\(", stripped, re.IGNORECASE)
                  and not re.search(r"\bGROUP\s+BY\b", stripped, re.IGNORECASE))
        if not is_agg:
            stripped = f"{stripped} LIMIT {CSV_ROW_LIMIT}"
    try:
        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(text(f"SET SESSION MAX_EXECUTION_TIME={DOWNLOAD_TIMEOUT_SECONDS * 1000}"))
            result = conn.execute(text(stripped))
            columns = list(result.keys())
            rows = [dict(zip(columns, row)) for row in result.fetchall()]
            return rows, ""
    except Exception as e:
        return [], str(e)


@trace_agent("v10.agent.executor")
def executor_node(state: GraphState) -> GraphState:
    raw_sql = state.get("validated_sql", "")
    sql = _bind_runtime_params(raw_sql, state)
    sql_h = _sql_hash(sql)  # hash AFTER binding so cache reflects actual query
    trace = state.get("agent_trace", [])

    freq = _track_query_frequency(sql_h)

    cached_rows = _check_materialized(sql_h)
    if cached_rows is not None:
        trace_entry = {
            "agent": "Executor",
            "status": "ok",
            "summary": f"Served from materialized cache · {len(cached_rows):,} row(s)",
            "detail": [f"Query frequency: {freq}x", "Source: materialized cache"],
        }
        trace = trace + [trace_entry]
        # OTEL attrs (cache hit path)
        try:
            from opentelemetry import trace as _otel_trace
            _sp = _otel_trace.get_current_span()
            if _sp is not None:
                _sp.set_attribute("executor.row_count", len(cached_rows))
                _sp.set_attribute("executor.was_cached", True)
        except Exception:
            pass
        return {**state, "query_result": cached_rows, "row_count": len(cached_rows),
                "execution_error": None, "agent_trace": trace}

    # Child span around the actual SQL execution
    from tracing import get_tracer as _get_tracer
    _tracer = _get_tracer()
    import time as _time
    _t0 = _time.perf_counter()
    with _tracer.start_as_current_span("v10.db.query") as _db_span:
        try:
            _db_span.set_attribute("db.system", "mysql")
            _db_span.set_attribute("db.statement_length", len(sql))
        except Exception:
            pass
        rows, error = execute_query(sql)
        try:
            _db_span.set_attribute("db.row_count", len(rows) if rows else 0)
            _db_span.set_attribute("db.elapsed_ms", round((_time.perf_counter() - _t0) * 1000, 2))
            if error:
                _db_span.set_attribute("db.error", str(error)[:200])
        except Exception:
            pass

    if error:
        trace_entry = {
            "agent": "Executor",
            "status": "error",
            "summary": "Query execution failed",
            "detail": [error[:200]],
        }
        trace = trace + [trace_entry]
        return {**state, "query_result": None, "row_count": 0, "execution_error": error, "agent_trace": trace}

    if freq >= 3 and len(rows) > 100:
        _save_materialized(sql_h, rows)

    detail = []
    if len(rows) >= ROW_LIMIT:
        detail.append(f"Results truncated at {ROW_LIMIT:,} rows (safety cap)")
    if _is_analytical_query(sql) and os.environ.get("DB_READ_REPLICA_HOST"):
        detail.append("Routed to read replica")
    if freq >= 3:
        detail.append(f"Query frequency: {freq}x (materialized for future requests)")

    trace_entry = {
        "agent": "Executor",
        "status": "ok",
        "summary": f"Query executed successfully · {len(rows):,} row(s) returned",
        "detail": detail,
    }
    trace = trace + [trace_entry]
    # OTEL attrs (live execution path)
    try:
        from opentelemetry import trace as _otel_trace
        _sp = _otel_trace.get_current_span()
        if _sp is not None:
            _sp.set_attribute("executor.row_count", len(rows))
            _sp.set_attribute("executor.was_cached", False)
    except Exception:
        pass
    return {**state, "query_result": rows, "row_count": len(rows), "execution_error": None, "agent_trace": trace}
