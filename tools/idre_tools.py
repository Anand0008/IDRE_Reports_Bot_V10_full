"""V10 MCP tools — read from knowledge/v10/, no static legacy artifacts."""
from __future__ import annotations
import json
import time
from pathlib import Path
from typing import Any

from sqlalchemy import text

KNOWLEDGE_DIR = Path(__file__).parent.parent / "knowledge" / "v10"


_business_logic_cache: dict | None = None
_schema_catalog_cache: dict | None = None
_enum_catalog_cache: dict | None = None
_glossary_cache: list | None = None


def _load(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _bl() -> dict:
    global _business_logic_cache
    if _business_logic_cache is None:
        _business_logic_cache = _load(KNOWLEDGE_DIR / "business_logic.json")
    return _business_logic_cache


def _schema() -> dict:
    global _schema_catalog_cache
    if _schema_catalog_cache is None:
        _schema_catalog_cache = _load(KNOWLEDGE_DIR / "schema_catalog.json")
    return _schema_catalog_cache


def _enums() -> dict:
    global _enum_catalog_cache
    if _enum_catalog_cache is None:
        _enum_catalog_cache = _load(KNOWLEDGE_DIR / "enum_catalog.json")
    return _enum_catalog_cache


def _glossary() -> list:
    global _glossary_cache
    if _glossary_cache is None:
        cfg = Path(__file__).parent.parent / "config" / "business_glossary.json"
        if cfg.exists():
            data = _load(cfg)
            _glossary_cache = data.get("terms", [])
        else:
            _glossary_cache = []
    return _glossary_cache


def _filter_patterns() -> dict:
    """Inline patterns (Day 7); could be a separate file later."""
    return {
        "today":            "DATE(:col) = DATE(:now)",
        "yesterday":        "DATE(:col) = DATE_SUB(DATE(:now), INTERVAL 1 DAY)",
        "this week":        ":col >= DATE_SUB(:now, INTERVAL WEEKDAY(:now) DAY)",
        "last 7 days":      ":col >= DATE_SUB(:now, INTERVAL 7 DAY)",
        "month-to-date":    ":col >= DATE_FORMAT(:now, '%Y-%m-01 00:00:00')",
        "mtd":              ":col >= DATE_FORMAT(:now, '%Y-%m-01 00:00:00')",
        "this month":       ":col >= DATE_FORMAT(:now, '%Y-%m-01 00:00:00')",
        "last month":       ":col >= DATE_FORMAT(:now - INTERVAL 1 MONTH, '%Y-%m-01') AND :col < DATE_FORMAT(:now, '%Y-%m-01')",
        "this quarter":     ":col >= DATE_FORMAT(DATE_SUB(:now, INTERVAL ((MONTH(:now)-1) MOD 3) MONTH), '%Y-%m-01')",
    }


# ─── Tool 1: get_idre_business_logic ────────────────────────────────

def get_idre_business_logic(report_name: str) -> str:
    """Get the full Prisma + JS post-processing + SQL equivalent for a known IDRE report."""
    rid = report_name.lower().strip()
    for r in _bl().get("reports", []):
        if r.get("id", "").lower() == rid:
            return json.dumps({
                "id": r["id"],
                "prisma_query": r.get("prisma_query", ""),
                "js_postprocessing": r.get("js_postprocessing", ""),
                "sql_equivalent": r.get("sql_equivalent", ""),
                "notes": r.get("notes", ""),
                "needs_review": r.get("needs_review", False),
            }, indent=2)
    available = [r["id"] for r in _bl().get("reports", [])]
    return f"Report '{report_name}' not found. Available: {', '.join(available)}"


# ─── Tool 2: get_table_schema ────────────────────────────────────────

def get_table_schema(table_name: str) -> str:
    """Get the schema for a single MySQL table — columns, types, optional flags."""
    name = table_name.lower().strip()
    for m in _schema().get("models", []):
        if m.get("table_name", "").lower() == name:
            return json.dumps(m, indent=2)
    available = sorted({m.get("table_name", "") for m in _schema().get("models", [])})
    return f"Table '{table_name}' not found. Available: {', '.join(available)}"


# ─── Tool 3: get_enum_values ─────────────────────────────────────────

def get_enum_values(column_path: str) -> str:
    """Get the valid enum values for a database column. Prefers rds_sampled source."""
    p = column_path.lower().strip().replace("`", "")
    rds = _enums().get("rds_sampled", {})
    if p in rds:
        return json.dumps({"source": "rds_sampled", "values": rds[p]}, indent=2)
    # fallback to TS enums
    ts = _enums().get("typescript_enums", {})
    for name, vals in ts.items():
        if name.lower() == p or p.endswith(name.lower()):
            return json.dumps({"source": "ts_enum", "name": name, "values": vals}, indent=2)
    return f"No enum mapping for '{column_path}'. Available rds-sampled: {', '.join(sorted(rds.keys()))}"


# ─── Tool 4: lookup_business_term ────────────────────────────────────

def lookup_business_term(term: str) -> str:
    """Look up a domain term in the IDRE glossary."""
    t = term.lower().strip()
    for e in _glossary():
        syns = [s.lower() for s in e.get("synonyms", [])]
        if e.get("term", "").lower() == t or t in syns:
            return json.dumps(e, indent=2)
    return f"Term '{term}' not found in glossary."


# ─── Tool 5: list_available_reports ──────────────────────────────────

def list_available_reports() -> str:
    """List all known IDRE reports with their endpoints."""
    out = []
    for r in _bl().get("reports", []):
        out.append({"id": r.get("id"), "endpoint": f"/api/reports/{r.get('id')}"})
    return json.dumps(out, indent=2)


# ─── Tool 6: find_filter_pattern ─────────────────────────────────────

def find_filter_pattern(intent: str) -> str:
    """Get the SQL date expression for a NL date phrase (today, mtd, last 7 days, etc.)."""
    intent_norm = intent.lower().strip()
    pats = _filter_patterns()
    if intent_norm in pats:
        return json.dumps({
            "intent": intent_norm,
            "sql_template": pats[intent_norm],
            "notes": "Replace :col with the actual datetime column; :now is supplied at execution time as the request anchor.",
        }, indent=2)
    return f"No pattern for '{intent}'. Known: {', '.join(pats.keys())}"


# ─── Tool 7: verify_sql_executes ─────────────────────────────────────

def verify_sql_executes(sql: str) -> str:
    """Run EXPLAIN then a LIMIT-5 dry run of the SQL on the read replica."""
    import sys
    bot_root = str(Path(__file__).parent.parent.resolve())
    if bot_root not in sys.path:
        sys.path.insert(0, bot_root)
    from db.connector import get_engine
    eng = get_engine()
    out: dict[str, Any] = {"sql": sql[:300], "ok": False}
    try:
        with eng.connect() as conn:
            t0 = time.monotonic()
            conn.execute(text(f"EXPLAIN {sql}"))
            test_sql = sql.rstrip(";\n ") + " LIMIT 5"
            rows = conn.execute(text(test_sql)).mappings().all()
            out["ok"] = True
            out["sample_row_count"] = len(rows)
            out["columns"] = list(rows[0].keys()) if rows else []
            out["exec_ms"] = round((time.monotonic() - t0) * 1000, 1)
    except Exception as e:
        out["error"] = str(e)[:500]
    return json.dumps(out, indent=2)


# ─── Tool Definitions for Gemini Function Calling ────────────────────

TOOL_DEFINITIONS = [
    {
        "name": "get_idre_business_logic",
        "description": "Get the full Prisma + JS post-processing + SQL equivalent for a known IDRE report. Call this FIRST if the user's question maps to any IDRE report.",
        "parameters": {"type": "object", "properties": {
            "report_name": {"type": "string", "description": "Report id, e.g. 'due-dates', 'outstanding-payments'."}
        }, "required": ["report_name"]},
    },
    {
        "name": "get_table_schema",
        "description": "Get the schema for a single MySQL table — column names, types, optional flags.",
        "parameters": {"type": "object", "properties": {
            "table_name": {"type": "string", "description": "Table name e.g. 'case' or 'payment'."}
        }, "required": ["table_name"]},
    },
    {
        "name": "get_enum_values",
        "description": "Get the valid enum values for a database column. Always prefer the rds_sampled source.",
        "parameters": {"type": "object", "properties": {
            "column_path": {"type": "string", "description": "Dot-notation e.g. 'case.status' or 'payment.type'."}
        }, "required": ["column_path"]},
    },
    {
        "name": "lookup_business_term",
        "description": "Look up a domain term in the IDRE glossary.",
        "parameters": {"type": "object", "properties": {
            "term": {"type": "string", "description": "Term e.g. 'CMS payment', 'outstanding'."}
        }, "required": ["term"]},
    },
    {
        "name": "list_available_reports",
        "description": "List all known IDRE reports with their endpoints.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "find_filter_pattern",
        "description": "Get the SQL date expression for a NL date phrase (today, mtd, last 7 days, etc.).",
        "parameters": {"type": "object", "properties": {
            "intent": {"type": "string", "description": "Date phrase e.g. 'month-to-date'."}
        }, "required": ["intent"]},
    },
    {
        "name": "verify_sql_executes",
        "description": "Run EXPLAIN then a LIMIT-5 dry run of the SQL on the read replica. Returns columns + sample row count or the error message. Call BEFORE returning the SQL as final.",
        "parameters": {"type": "object", "properties": {
            "sql": {"type": "string", "description": "MySQL SELECT statement to validate."}
        }, "required": ["sql"]},
    },
]

TOOL_DISPATCH = {
    "get_idre_business_logic": get_idre_business_logic,
    "get_table_schema": get_table_schema,
    "get_enum_values": get_enum_values,
    "lookup_business_term": lookup_business_term,
    "list_available_reports": list_available_reports,
    "find_filter_pattern": find_filter_pattern,
    "verify_sql_executes": verify_sql_executes,
}
