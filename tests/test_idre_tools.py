"""Tests for tools/idre_tools.py (Node 6 cleanup).

Covers:
  - Phase 1 (I1): get_report_reference alias removed; 7 distinct tools.
  - Phase 3 (I3): verify_sql_executes resolves bot_root from __file__,
    no cwd switch.
"""
import os
import sys
from pathlib import Path

import tools.idre_tools as it


# ── Phase 1 (I1): alias removal ──────────────────────────────────────

def test_get_report_reference_alias_removed():
    """The V8-vintage backward-compatibility alias has no V10 caller and is
    gone from both the dispatch table and the Gemini tool definitions."""
    assert "get_report_reference" not in it.TOOL_DISPATCH, (
        "get_report_reference must no longer appear in TOOL_DISPATCH"
    )
    names = [d["name"] for d in it.TOOL_DEFINITIONS]
    assert "get_report_reference" not in names, (
        "get_report_reference must no longer appear in TOOL_DEFINITIONS"
    )
    assert not hasattr(it, "get_report_reference"), (
        "get_report_reference function must be deleted from the module"
    )


def test_tool_inventory_has_seven_distinct_tools():
    """V10 ships exactly 7 distinct callable tools after the alias drop."""
    assert len(it.TOOL_DISPATCH) == 7, (
        f"TOOL_DISPATCH should expose 7 tools, got {len(it.TOOL_DISPATCH)}: "
        f"{sorted(it.TOOL_DISPATCH.keys())}"
    )
    assert len(it.TOOL_DEFINITIONS) == 7, (
        f"TOOL_DEFINITIONS should expose 7 tools, got {len(it.TOOL_DEFINITIONS)}"
    )
    expected = {
        "get_idre_business_logic",
        "get_table_schema",
        "get_enum_values",
        "lookup_business_term",
        "list_available_reports",
        "find_filter_pattern",
        "verify_sql_executes",
    }
    assert set(it.TOOL_DISPATCH.keys()) == expected
    assert {d["name"] for d in it.TOOL_DEFINITIONS} == expected


# ── Phase 3 (I3): verify_sql_executes path resolution ────────────────

def test_verify_sql_executes_resolves_bot_root_from_file(monkeypatch):
    """verify_sql_executes must derive bot_root from __file__ — no
    hardcoded user-specific path, no os.chdir side effect."""
    expected_bot_root = str(Path(it.__file__).parent.parent.resolve())

    captured: dict = {"engine_calls": 0, "cwd_during_call": None}

    class _FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, *_a, **_kw):
            class _R:
                def mappings(self):
                    return self

                def all(self):
                    return []

            return _R()

    class _FakeEngine:
        def connect(self):
            return _FakeConn()

    def _fake_get_engine():
        captured["engine_calls"] += 1
        captured["cwd_during_call"] = os.getcwd()
        return _FakeEngine()

    # Stub the lazy import inside verify_sql_executes
    import types

    fake_mod = types.ModuleType("db.connector")
    fake_mod.get_engine = _fake_get_engine
    monkeypatch.setitem(sys.modules, "db.connector", fake_mod)
    fake_pkg = types.ModuleType("db")
    fake_pkg.connector = fake_mod
    monkeypatch.setitem(sys.modules, "db", fake_pkg)

    cwd_before = os.getcwd()
    out = it.verify_sql_executes("SELECT 1")

    assert captured["engine_calls"] == 1, "expected one get_engine() call"
    # cwd must NOT have been changed by verify_sql_executes
    assert captured["cwd_during_call"] == cwd_before, (
        "verify_sql_executes must not chdir into bot_root"
    )
    assert os.getcwd() == cwd_before, "cwd must be unchanged after the call"
    # bot_root must be on sys.path (module-relative resolution)
    assert expected_bot_root in sys.path, (
        f"sys.path must include __file__-derived bot_root: {expected_bot_root}"
    )
    # No hardcoded user path should ever be inserted by this function
    assert "<HOME>/Downloads/v10_reports_bot" not in sys.path or (
        Path("<HOME>/Downloads/v10_reports_bot").resolve()
        == Path(expected_bot_root).resolve()
    ), (
        "verify_sql_executes must not rely on the hardcoded developer-machine path"
    )
    # Sanity: it returned a JSON string
    import json as _json

    parsed = _json.loads(out)
    assert "ok" in parsed
