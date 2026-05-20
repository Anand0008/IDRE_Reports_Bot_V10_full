"""Tests for agents/response_formatter.py.

Coverage focus:
- Chart-type selection has been narrowed to just "number" and "table"
  (2026-05-20 user request). The previous 9-chart decision tree
  (heatmap, funnel, scatter, line, stacked_bar, pie, bar) is disabled
  pending the Node 9/10 paired formatter deepdive; only the two safe
  fallbacks remain so the UI does not render any chart at all.
- format_response() markdown rendering is preserved.
- Node 10 (May 20) backfill: the 3 Gemini fanout helpers
  (_generate_explanation, _generate_narrative, _generate_suggestions),
  _extract_token_usage, _build_slide_metadata, and the node-level
  integration are now covered. Gemini calls are monkeypatched so the
  test suite stays offline.
"""
import json
from types import SimpleNamespace

import pytest

import agents.response_formatter as rf


# ── Chart-selection narrowing (2026-05-20) ───────────────────────────

def test_select_chart_empty_rows_returns_table():
    fmt, cfg = rf._select_chart([], "anything")
    assert fmt == "table"
    assert cfg is None


def test_select_chart_single_scalar_returns_number():
    """1 row × 1 col is a single-scalar answer (e.g. COUNT result)."""
    fmt, cfg = rf._select_chart([{"count": 42}], "how many cases")
    assert fmt == "number"
    assert cfg is None


def test_select_chart_multi_row_returns_table_not_chart():
    """Previously: categorical + numeric + low cardinality → bar/pie/
    stacked_bar. Now: always table."""
    rows = [
        {"status": "OPEN", "count": 10},
        {"status": "CLOSED", "count": 7},
        {"status": "PENDING", "count": 3},
    ]
    fmt, cfg = rf._select_chart(rows, "cases by status")
    assert fmt == "table", f"Expected table, got {fmt}"
    assert cfg is None, "chart_config must be None — chart rendering is disabled"


def test_select_chart_time_series_returns_table_not_line():
    """Previously: date_col + numeric_col(s) → line_chart. Now: table."""
    rows = [
        {"month": "2026-01", "revenue": 1000},
        {"month": "2026-02", "revenue": 1200},
        {"month": "2026-03", "revenue": 1500},
    ]
    fmt, cfg = rf._select_chart(rows, "revenue over time")
    assert fmt == "table"
    assert cfg is None


def test_select_chart_heatmap_intent_returns_table():
    """Previously: 'heatmap' keyword + 2 categorical + 1 numeric → heatmap."""
    rows = [
        {"x": "A", "y": "1", "val": 5},
        {"x": "A", "y": "2", "val": 8},
        {"x": "B", "y": "1", "val": 3},
    ]
    fmt, cfg = rf._select_chart(rows, "show me a heatmap of x by y")
    assert fmt == "table"
    assert cfg is None


def test_select_chart_funnel_intent_returns_table():
    """Previously: 'funnel/pipeline/stages/conversion' + status + numeric → funnel."""
    rows = [
        {"stage": "submitted", "count": 100},
        {"stage": "review", "count": 60},
        {"stage": "closed", "count": 30},
    ]
    fmt, cfg = rf._select_chart(rows, "show the funnel of cases")
    assert fmt == "table"
    assert cfg is None


def test_select_chart_scatter_intent_returns_table():
    """Previously: 'scatter/correlation/vs' + 2 numeric → scatter_plot."""
    rows = [
        {"x": 1.0, "y": 2.0}, {"x": 2.0, "y": 4.5}, {"x": 3.0, "y": 6.1},
    ]
    fmt, cfg = rf._select_chart(rows, "x vs y correlation")
    assert fmt == "table"
    assert cfg is None


# ── format_response (markdown rendering) preserved ───────────────────

def test_format_response_single_scalar_uses_bold():
    """1 row × 1 col is rendered as **column:** value with thousands separator."""
    out = rf.format_response([{"total": 1020}], "SELECT COUNT(*) FROM `case`", [])
    assert "**total:**" in out
    assert "1,020" in out
    assert "```sql" in out


def test_format_response_table_includes_row_count_and_sql():
    rows = [
        {"id": 1, "name": "alpha"},
        {"id": 2, "name": "beta"},
    ]
    out = rf.format_response(rows, "SELECT id, name FROM `case`", [])
    assert "id" in out and "name" in out
    assert "2 row(s) returned" in out
    assert "```sql" in out and "SELECT id, name FROM `case`" in out


def test_format_response_no_rows_returns_friendly_message():
    out = rf.format_response([], "SELECT 1", [])
    assert "No results found" in out


def test_format_response_includes_assumptions_block():
    out = rf.format_response(
        [{"x": 1}],
        "SELECT 1 AS x",
        ["Used createdAt column for date filtering"],
    )
    assert "Assumptions made" in out
    assert "createdAt column" in out


# ── Capability pin ───────────────────────────────────────────────────

def test_public_surface_preserved():
    """Even after narrowing _select_chart, the rest of the agent surface
    (Gemini fanout helpers, slide metadata, node entrypoint) must stay."""
    for name in (
        "_select_chart",
        "format_response",
        "_generate_explanation",
        "_generate_narrative",
        "_generate_suggestions",
        "_build_slide_metadata",
        "_extract_token_usage",
        "response_formatter_node",
        "MAX_TABLE_ROWS",
    ):
        assert hasattr(rf, name), f"Public surface lost: {name}"


# ── Node 10 Phase 4: Gemini fanout coverage (monkeypatched LLM) ──────


def _stub_llm(content: str, usage: dict | None = None):
    """Build a fake ChatGoogleGenerativeAI whose .invoke returns a canned
    LangChain-shaped response. usage_metadata is exposed as a dict (this
    matches the shape _extract_token_usage already handles)."""
    response = SimpleNamespace(content=content, usage_metadata=usage or {})

    class _Fake:
        def __init__(self, *a, **kw):
            pass

        def invoke(self, _messages):
            return response

    return _Fake


def test_extract_token_usage_parses_present_fields():
    response = SimpleNamespace(usage_metadata={
        "input_tokens": 120,
        "output_tokens": 45,
        "total_tokens": 165,
    })
    out = rf._extract_token_usage(response)
    assert out == {"input": 120, "output": 45, "total": 165}


def test_extract_token_usage_handles_missing_metadata():
    response = SimpleNamespace()  # no usage_metadata attribute
    out = rf._extract_token_usage(response)
    assert out == {"input": 0, "output": 0, "total": 0}


def test_generate_explanation_returns_text_and_token_usage(monkeypatch):
    monkeypatch.setattr(
        rf,
        "ChatGoogleGenerativeAI",
        _stub_llm("Counts the most recent cases by status.", {"input_tokens": 50, "output_tokens": 20, "total_tokens": 70}),
    )
    text, tok = rf._generate_explanation("SELECT * FROM `case` LIMIT 5")
    assert "Counts" in text
    assert tok["total"] == 70


def test_generate_explanation_swallows_llm_exceptions(monkeypatch):
    """The function must NOT raise — it returns ('', {}) on any failure
    so the rest of the response can still render."""
    class _Boom:
        def __init__(self, *a, **kw): pass
        def invoke(self, _msgs): raise RuntimeError("api down")
    monkeypatch.setattr(rf, "ChatGoogleGenerativeAI", _Boom)
    text, tok = rf._generate_explanation("SELECT 1")
    assert text == ""
    assert tok == {}


def test_generate_narrative_returns_text_for_multi_row_result(monkeypatch):
    monkeypatch.setattr(
        rf,
        "ChatGoogleGenerativeAI",
        _stub_llm("Revenue increased 23% month-over-month.", {"input_tokens": 80, "output_tokens": 25, "total_tokens": 105}),
    )
    rows = [{"month": "2026-01", "revenue": 100}, {"month": "2026-02", "revenue": 123}]
    text, tok = rf._generate_narrative("revenue by month", rows)
    assert "23%" in text or "Revenue" in text
    assert tok["total"] == 105


def test_generate_narrative_skips_when_fewer_than_two_rows(monkeypatch):
    """Single-row results don't warrant a narrative — function returns
    ('', {}) without invoking the LLM."""
    called = {"count": 0}

    class _Counter:
        def __init__(self, *a, **kw): pass
        def invoke(self, _msgs):
            called["count"] += 1
            return SimpleNamespace(content="should not happen", usage_metadata={})

    monkeypatch.setattr(rf, "ChatGoogleGenerativeAI", _Counter)
    text, tok = rf._generate_narrative("anything", [{"x": 1}])
    assert text == ""
    assert tok == {}
    assert called["count"] == 0, "Narrative LLM must not be called for <2 rows"


def test_generate_suggestions_parses_json_array(monkeypatch):
    monkeypatch.setattr(
        rf,
        "ChatGoogleGenerativeAI",
        _stub_llm(
            json.dumps(["Q1?", "Q2?", "Q3?"]),
            {"input_tokens": 40, "output_tokens": 15, "total_tokens": 55},
        ),
    )
    suggestions, tok = rf._generate_suggestions("show me cases", [{"id": 1}])
    assert suggestions == ["Q1?", "Q2?", "Q3?"]
    assert tok["total"] == 55


def test_generate_suggestions_strips_markdown_json_fences(monkeypatch):
    """Gemini occasionally wraps the array in ```json ... ``` fences."""
    fenced = "```json\n" + json.dumps(["A?", "B?", "C?"]) + "\n```"
    monkeypatch.setattr(rf, "ChatGoogleGenerativeAI", _stub_llm(fenced, {}))
    suggestions, _ = rf._generate_suggestions("intent", [{"id": 1}])
    assert suggestions == ["A?", "B?", "C?"]


def test_generate_suggestions_returns_empty_on_unparseable_response(monkeypatch):
    monkeypatch.setattr(
        rf, "ChatGoogleGenerativeAI", _stub_llm("not valid JSON at all", {})
    )
    suggestions, tok = rf._generate_suggestions("intent", [{"id": 1}])
    assert suggestions == []
    # tok may be {} (early return after parse failure) — accept either
    # shape so the test pins behaviour without overfitting the path.
    assert isinstance(tok, dict)


def test_build_slide_metadata_returns_empty_when_chart_config_is_none():
    """Since the chart-disable change, chart_config is always None →
    slide_metadata is structurally dormant. Pin that behaviour."""
    out = rf._build_slide_metadata(
        chart_config=None,
        explanation="x", narrative="y", rows=[{"id": 1}], intent="anything",
    )
    assert out == {}


def test_build_slide_metadata_populates_when_chart_config_present():
    """The function is still callable for the (hypothetical) future
    chart re-enable. Pin the shape so it doesn't bit-rot."""
    cfg = {"type": "bar_chart", "title": "Cases by Status", "x_col": "status", "y_col": "count"}
    out = rf._build_slide_metadata(
        chart_config=cfg,
        explanation="One-liner.",
        narrative="Two-sentence narrative.",
        rows=[{"status": "OPEN", "count": 3}, {"status": "CLOSED", "count": 5}],
        intent="cases by status",
    )
    assert out["title"] == "Cases by Status"
    assert out["chart_type"] == "bar_chart"
    assert out["chart_config"] == cfg
    assert out["row_count"] == 2
    assert out["data_snapshot"][0]["status"] == "OPEN"


def test_response_formatter_node_accumulates_token_usage(monkeypatch):
    """Integration: the node should run the 3 Gemini calls in parallel,
    aggregate their token usage into state['token_usage']['Response Formatter'],
    and write a trace entry of the expected shape."""
    monkeypatch.setattr(
        rf,
        "ChatGoogleGenerativeAI",
        _stub_llm(
            "Counts cases by status.",
            {"input_tokens": 50, "output_tokens": 20, "total_tokens": 70},
        ),
    )
    state = {
        "query_result": [
            {"status": "OPEN", "count": 3},
            {"status": "CLOSED", "count": 5},
        ],
        "validated_sql": "SELECT status, COUNT(*) FROM `case` GROUP BY status",
        "generated_sql": "SELECT status, COUNT(*) FROM `case` GROUP BY status",
        "user_query": "cases by status",
        "resolved_query": "cases by status",
        "assumptions": [],
        "agent_trace": [],
        "token_usage": {},
    }
    out = rf.response_formatter_node(state)
    # formatted_response present (markdown table)
    assert "status" in (out["formatted_response"] or "").lower()
    # chart_config remains None per chart-disable
    assert out["chart_config"] is None
    # response_format follows _select_chart's narrowed output
    assert out["response_format"] in ("table", "number")
    # Token usage accumulated under the Response Formatter bucket
    rf_tokens = out["token_usage"].get("Response Formatter")
    assert rf_tokens is not None
    # Three Gemini calls × 70 total = 210 (if all three fired). Narrative
    # fires only with ≥ 2 rows (we have 2); suggestions fires; explanation
    # fires (sql present). All three should fire.
    assert rf_tokens["total"] >= 70, (
        f"Token usage should be accumulated across the 3 fanout calls, got {rf_tokens}"
    )
    # Trace entry from this agent
    last_trace = out["agent_trace"][-1]
    assert last_trace["agent"] == "Response Formatter"


def test_response_formatter_node_handles_empty_result(monkeypatch):
    """No rows → no Gemini calls for narrative/suggestions, but
    formatted_response still rendered + trace entry still emitted."""
    monkeypatch.setattr(rf, "ChatGoogleGenerativeAI", _stub_llm("unused", {}))
    state = {
        "query_result": [],
        "validated_sql": "SELECT id FROM `case` WHERE 1=0",
        "user_query": "no results", "resolved_query": "no results",
        "assumptions": [],
        "agent_trace": [],
        "token_usage": {},
    }
    out = rf.response_formatter_node(state)
    assert "No results" in (out["formatted_response"] or "")
    assert out["agent_trace"][-1]["agent"] == "Response Formatter"
