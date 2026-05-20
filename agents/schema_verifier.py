"""
Schema Verifier Agent — V10 (post-Node-5)

After schema_mapper picks the candidate tables, this agent fetches
their live column lists + index info via SHOW COLUMNS / SHOW INDEX
against MySQL (cached per-process). Annotates each column with
INDEXED markers so the SQL Writer can prefer indexed columns in
WHERE clauses. Detects schema drift against a cached snapshot
(data/schema_snapshot.json) and surfaces additions/removals in the
trace.

History:
- V5+: introduced. Live SHOW COLUMNS + hand-curated
  `_COMMONLY_HALLUCINATED` dict of bad column names with explicit
  "does NOT have" warnings.
- V6: added Levenshtein column suggestions, schema diff detection,
  index awareness, and a per-column access-frequency annotation. The
  frequency annotation was dead-on-arrival — its input JSON file under
  data/ was never written by any version.
- V8: `verify_sql_executes` tool introduced. The LLM now catches
  hallucinated columns reactively via EXPLAIN dry-run; the
  pre-emptive `_COMMONLY_HALLUCINATED` warnings became redundant.
- V10 Node 2/3: removed the `_COMMONLY_HALLUCINATED` dict + its
  warning code path. The Levenshtein column suggester became
  orphaned at that point — defined but no callers.
- V10 Node 5 (2026-05-20): removed the orphaned Levenshtein
  suggester + its helper, the dead-on-arrival access-frequency
  loader + its path constant, and the per-column frequency
  annotation in build_verified_schema. See
  `local/docs/superpowers/reports/2026-05-20-node5-history-summary.md`.
"""
import json
import os
import re
from sqlalchemy import text
from db.connector import get_engine
from state.context import GraphState
from tracing import trace_agent

_DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
_SCHEMA_SNAPSHOT_PATH = os.path.join(_DATA_DIR, "schema_snapshot.json")

_column_cache: dict[str, list[dict]] = {}
_index_cache: dict[str, list[dict]] = {}


def _fetch_columns(table_name: str) -> list[dict]:
    if table_name in _column_cache:
        return _column_cache[table_name]
    try:
        engine = get_engine()
        with engine.connect() as conn:
            result = conn.execute(text(f"SHOW COLUMNS FROM `{table_name}`"))
            cols = []
            for row in result.fetchall():
                cols.append({
                    "name": row[0],
                    "type": row[1],
                    "nullable": row[2] == "YES",
                    "key": row[3] or "",
                })
            _column_cache[table_name] = cols
            return cols
    except Exception:
        return []


def _fetch_indexes(table_name: str) -> list[dict]:
    if table_name in _index_cache:
        return _index_cache[table_name]
    try:
        engine = get_engine()
        with engine.connect() as conn:
            result = conn.execute(text(f"SHOW INDEX FROM `{table_name}`"))
            indexes = []
            for row in result.fetchall():
                indexes.append({
                    "key_name": row[2],
                    "column_name": row[4],
                    "non_unique": row[1],
                })
            _index_cache[table_name] = indexes
            return indexes
    except Exception:
        return []


def _load_schema_snapshot() -> dict:
    if not os.path.exists(_SCHEMA_SNAPSHOT_PATH):
        return {}
    try:
        with open(_SCHEMA_SNAPSHOT_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_schema_snapshot(snapshot: dict) -> None:
    os.makedirs(os.path.dirname(_SCHEMA_SNAPSHOT_PATH), exist_ok=True)
    with open(_SCHEMA_SNAPSHOT_PATH, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2)


def _detect_schema_diff(table: str, current_cols: list[dict]) -> list[str]:
    snapshot = _load_schema_snapshot()
    cached = snapshot.get(table, [])
    if not cached:
        return []
    cached_names = {c["name"] for c in cached}
    current_names = {c["name"] for c in current_cols}
    added = current_names - cached_names
    removed = cached_names - current_names
    diffs = []
    if added:
        diffs.append(f"New columns in `{table}`: {', '.join(added)}")
    if removed:
        diffs.append(f"Removed columns from `{table}`: {', '.join(removed)}")
    return diffs


def build_verified_schema(tables: list[str]) -> str:
    blocks = []
    schema_diffs = []
    snapshot_update = {}
    for table in tables:
        cols = _fetch_columns(table)
        if not cols:
            continue

        snapshot_update[table] = [{"name": c["name"], "type": c["type"]} for c in cols]
        diffs = _detect_schema_diff(table, cols)
        schema_diffs.extend(diffs)

        indexes = _fetch_indexes(table)
        indexed_cols = {idx["column_name"] for idx in indexes}

        col_lines = []
        for c in cols:
            key_info = f" [{c['key']}]" if c['key'] else ""
            null_info = " NULL" if c['nullable'] else " NOT NULL"
            annotations = []
            if c["name"] in indexed_cols:
                annotations.append("INDEXED")
            ann_str = f" ({', '.join(annotations)})" if annotations else ""
            col_lines.append(f"  - {c['name']} ({c['type']}{null_info}{key_info}){ann_str}")

        block = f"=== VERIFIED COLUMNS: `{table}` ===\n"
        block += "\n".join(col_lines)

        if indexed_cols:
            block += f"\n  INDEXED columns: {', '.join(sorted(indexed_cols))}"
            block += "\n  TIP: Use indexed columns in WHERE clauses for better performance."

        blocks.append(block)

    if snapshot_update:
        try:
            existing = _load_schema_snapshot()
            existing.update(snapshot_update)
            _save_schema_snapshot(existing)
        except OSError:
            pass

    if not blocks:
        return ""

    header = "--- LIVE SCHEMA (verified via SHOW COLUMNS) ---\n\n"
    if schema_diffs:
        header += "⚠ SCHEMA CHANGES DETECTED:\n" + "\n".join(f"  {d}" for d in schema_diffs) + "\n\n"

    return header + "\n\n".join(blocks)


@trace_agent("v10.agent.schema_verifier")
def schema_verifier_node(state: GraphState) -> GraphState:
    tables = state.get("relevant_tables", [])
    if not tables:
        return state

    verified = build_verified_schema(tables)
    if not verified:
        trace_entry = {
            "agent": "Schema Verifier",
            "status": "warn",
            "summary": "Could not verify schema — DB may be unreachable",
            "detail": [],
        }
        trace = state.get("agent_trace", []) + [trace_entry]
        return {**state, "agent_trace": trace}

    existing_ctx = state.get("schema_context", "")
    enriched = verified + "\n\n" + existing_ctx

    negatives_count = verified.count("does NOT have")
    suggestions_count = verified.count("SUGGESTION:")
    index_count = verified.count("INDEXED columns:")
    schema_changes = verified.count("SCHEMA CHANGES DETECTED")

    detail = [f"Tables verified: {', '.join(tables)}"]
    if suggestions_count:
        detail.append(f"{suggestions_count} column suggestion(s) provided")
    if index_count:
        detail.append(f"Index info included for {index_count} table(s)")
    if schema_changes:
        detail.append("Schema changes detected since last snapshot")

    trace_entry = {
        "agent": "Schema Verifier",
        "status": "ok",
        "summary": f"Verified columns for {len(tables)} table(s) · {negatives_count} warning(s) · {suggestions_count} suggestion(s)",
        "detail": detail,
    }
    trace = state.get("agent_trace", []) + [trace_entry]

    return {**state, "schema_context": enriched, "agent_trace": trace}
