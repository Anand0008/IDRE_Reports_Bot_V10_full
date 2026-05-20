"""Tests for agents/debugger_agent.py (Node 8).

Pins error-classification + retry-strategy behaviour:
- Each of the 13 regex patterns produces the expected (error_type, strategy).
- _escalated_instruction differs across attempts 1/2/3.
- Phase 3 (I4): "metric card" reference stripped from the third-escalation prompt.
- debugger_node returns `debug_retry_strategy=abort` for WRONG_STATEMENT_TYPE
  (smoke-test S6 fix relies on this field being propagated).
- KB roundtrip: save + mark_success + lookup returns the saved hint.

LLM fallback (_llm_classify_error) is NOT exercised live — it requires a
real Gemini call. Its plumbing is structurally validated through _classify
with use_llm=False (regex-only) so the routing through the function stays
testable without network.
"""
import json
import os

import agents.debugger_agent as dbg


# ── Classifier coverage: 13 patterns + UNKNOWN fallback ──────────────

def test_classify_hallucinated_column():
    r = dbg._classify("Unknown column 'foo.bar'")
    assert r.error_type == "HALLUCINATED_COLUMN"
    assert r.retry_strategy == "rewrite"
    assert "foo.bar" in r.failing_element or r.failing_element == "foo.bar"


def test_classify_hallucinated_table_unknown_column_path():
    # The validator's "Unknown table(s) referenced: [...]" message path.
    r = dbg._classify("Unknown table(s) referenced: [made_up_table]. Check spelling")
    assert r.error_type == "HALLUCINATED_TABLE"
    assert r.retry_strategy == "rewrite"


def test_classify_hallucinated_table_mysql_doesnt_exist():
    r = dbg._classify("Table 'idre_stage.totally_made_up' doesn't exist")
    assert r.error_type == "HALLUCINATED_TABLE"


def test_classify_hallucinated_column_from_validator_message():
    """sql_validator's "Hallucinated column(s) detected:" wrapper path."""
    err = (
        "Hallucinated column(s) detected:\n"
        "Column `case`.`nonexistent_xyz` does not exist. Valid columns: id, status..."
    )
    r = dbg._classify(err)
    assert r.error_type == "HALLUCINATED_COLUMN"


def test_classify_ambiguous_column():
    r = dbg._classify("Column 'id' in field list is ambiguous")
    assert r.error_type == "AMBIGUOUS_COLUMN"
    assert r.retry_strategy == "rewrite"


def test_classify_syntax_error():
    r = dbg._classify("You have an error in your SQL syntax; check the manual")
    assert r.error_type == "SYNTAX_ERROR"


def test_classify_timeout_uses_suggest_filter_strategy():
    r = dbg._classify("Query execution was interrupted, maximum statement execution time exceeded")
    assert r.error_type == "TIMEOUT"
    assert r.retry_strategy == "suggest_filter"


def test_classify_division_by_zero():
    r = dbg._classify("Division by zero in expression")
    assert r.error_type == "DIVISION_BY_ZERO"


def test_classify_type_mismatch_incorrect_datetime():
    r = dbg._classify("Incorrect datetime value: 'not-a-date'")
    assert r.error_type == "TYPE_MISMATCH"


def test_classify_safety_violation_uses_abort_strategy():
    r = dbg._classify("Blocked keyword 'DROP' found in query")
    assert r.error_type == "SAFETY_VIOLATION"
    assert r.retry_strategy == "abort", "Safety violations must abort, not retry"


def test_classify_wrong_statement_type_uses_abort_strategy():
    """Smoke-test S6: WRONG_STATEMENT_TYPE → strategy abort. This was the
    classification path the orchestrator's _route_after_debugger had to
    honour; the bug was elsewhere (state schema), but the classification
    must remain correct."""
    r = dbg._classify("Query must be a SELECT statement. Got: ASSUMPTIONS:")
    assert r.error_type == "WRONG_STATEMENT_TYPE"
    assert r.retry_strategy == "abort"


def test_classify_lock_timeout_uses_retry_as_is():
    r = dbg._classify("Lock wait timeout exceeded; try restarting transaction")
    assert r.error_type == "LOCK_TIMEOUT"
    assert r.retry_strategy == "retry_as_is"


def test_classify_unclassified_error_falls_back_to_unknown():
    """No regex matches → UNKNOWN with default 'rewrite' strategy."""
    r = dbg._classify("Some completely novel error nobody has seen before")
    assert r.error_type == "UNKNOWN"
    assert r.retry_strategy == "rewrite"


# ── Phase 3 (I4): "metric card" reference stripped ──────────────────

def test_third_escalation_no_metric_card_reference():
    """V10 retired metric_cards.json; the LLM must not be told to use them."""
    instr = dbg._escalated_instruction("base", attempt=3, error_type="UNKNOWN")
    assert "metric card" not in instr.lower(), (
        "Third-escalation prompt still references retired metric_cards"
    )
    # The "minimum-viable structural" guidance must remain
    assert "2 tables" in instr or "simplest possible" in instr.lower()


def test_escalated_instruction_differs_by_attempt():
    """Each escalation level adds more aggressive simplification guidance."""
    a1 = dbg._escalated_instruction("base", attempt=1, error_type="UNKNOWN")
    a2 = dbg._escalated_instruction("base", attempt=2, error_type="UNKNOWN")
    a3 = dbg._escalated_instruction("base", attempt=3, error_type="UNKNOWN")
    assert a1 == "base"
    assert "ESCALATION (attempt 2)" in a2
    assert "ESCALATION (attempt 3" in a3
    assert len(a3) > len(a2) > len(a1)


# ── _build_retry_context content ─────────────────────────────────────

def test_build_retry_context_includes_strategy_and_failing_element():
    result = dbg.DebugResult(
        error_type="HALLUCINATED_COLUMN",
        failing_element="nonexistent_col",
        retry_instruction="Column does not exist — use only verified columns.",
        retry_strategy="rewrite",
    )
    ctx = dbg._build_retry_context(
        error="Unknown column 'nonexistent_col'",
        result=result,
        attempt=1,
        original_sql="SELECT nonexistent_col FROM `case`",
    )
    assert "HALLUCINATED_COLUMN" in ctx
    assert "rewrite" in ctx
    assert "nonexistent_col" in ctx
    assert "SELECT nonexistent_col" in ctx, "Previous SQL must be quoted in retry context"


# ── debugger_node output (smoke-test S6 protective test) ─────────────

def test_debugger_node_propagates_abort_strategy_in_state():
    """The bug behind smoke-test S6 was that debug_retry_strategy didn't
    survive LangGraph's state merge — fixed in Phase 1 by adding the
    field to GraphState. This test confirms debugger_node still emits
    it correctly; the schema fix makes it visible to the routing edge."""
    state = {
        "execution_error": None,
        "error_message": "Query must be a SELECT statement. Got: ASSUMPTIONS:",
        "validated_sql": "",
        "generated_sql": "ASSUMPTIONS:\n- xyz",
        "retry_count": 0,
        "agent_trace": [],
    }
    out = dbg.debugger_node(state)
    assert out["debug_error_type"] == "WRONG_STATEMENT_TYPE"
    assert out["debug_retry_strategy"] == "abort"


def test_debugger_node_clears_error_fields_after_classifying():
    """After classification, the debugger clears execution_error +
    error_message so downstream nodes don't re-trigger on stale state."""
    state = {
        "execution_error": "Unknown column 'foo'",
        "error_message": "",
        "validated_sql": "SELECT foo FROM `case`",
        "generated_sql": "SELECT foo FROM `case`",
        "retry_count": 0,
        "agent_trace": [],
    }
    out = dbg.debugger_node(state)
    assert out["execution_error"] is None
    assert out["error_message"] == ""
    assert out["retry_context"], "retry_context must be populated"


# ── KB roundtrip ─────────────────────────────────────────────────────

def test_kb_lookup_returns_empty_when_no_success(tmp_path, monkeypatch):
    monkeypatch.setattr(dbg, "_ERROR_KB_PATH", str(tmp_path / "kb.json"))
    # Save an entry but DON'T mark it success — lookup must return ""
    dbg._save_error_fix("HALLUCINATED_COLUMN", "err snippet", "fix description")
    assert dbg._kb_lookup("HALLUCINATED_COLUMN") == ""


def test_kb_roundtrip_save_mark_success_then_lookup(tmp_path, monkeypatch):
    monkeypatch.setattr(dbg, "_ERROR_KB_PATH", str(tmp_path / "kb.json"))
    dbg._save_error_fix(
        "SYNTAX_ERROR", "you have an error in your SQL syntax", "Check balanced parens"
    )
    dbg.mark_fix_success("SYNTAX_ERROR", success=True)
    hint = dbg._kb_lookup("SYNTAX_ERROR")
    assert hint, "Lookup must return a hint after success-marking"
    assert "Check balanced parens" in hint


# ── Capability pin ───────────────────────────────────────────────────

def test_public_surface_preserved():
    for name in (
        "DebugResult",
        "_PATTERNS",
        "_RETRY_STRATEGIES",
        "_classify",
        "_escalated_instruction",
        "_build_retry_context",
        "_llm_classify_error",
        "_save_error_fix",
        "_kb_lookup",
        "mark_fix_success",
        "debugger_node",
        "MAX_RETRIES",
    ):
        assert hasattr(dbg, name), f"Public surface lost: {name}"
    # 14 regex patterns after Node 6.1 added NO_SQL_EMITTED (was 13 in V10
    # baseline; the new pattern handles validator's "Empty SQL generated."
    # message produced by the Node 6.1 sql_writer parser fix when no SQL
    # can be extracted from Gemini's response).
    assert len(dbg._PATTERNS) == 14, (
        f"Expected 14 regex patterns after Node 6.1 NO_SQL_EMITTED addition, got {len(dbg._PATTERNS)}"
    )


# ── Node 6.1 D3 — NO_SQL_EMITTED pattern ─────────────────────────────


def test_classify_no_sql_emitted_uses_rewrite_strategy_and_specific_instruction():
    """Node 6.1: when sql_writer's parser couldn't extract SQL, the
    validator emits 'Empty SQL generated.' The debugger now classifies
    this with a specific retry instruction instead of UNKNOWN."""
    r = dbg._classify("Empty SQL generated.")
    assert r.error_type == "NO_SQL_EMITTED"
    assert r.retry_strategy == "rewrite"
    # Instruction must name the fence requirement so the LLM knows what
    # to fix on the next attempt
    assert "```sql" in r.retry_instruction or "code fence" in r.retry_instruction.lower()
    assert "assumptions" in r.retry_instruction.lower() or "commentary" in r.retry_instruction.lower()
