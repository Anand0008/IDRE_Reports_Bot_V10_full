"""Tests for utils/feedback_store.py — verify the 2026-05-21
reproducibility enrichment mirrors AuditEvent's field set."""
from dataclasses import fields, asdict

from utils import feedback_store as fs


def test_feedback_record_has_all_reproducibility_fields():
    """The 7 reproducibility fields it was missing must now be declared."""
    field_names = {f.name for f in fields(fs.FeedbackRecord)}
    required = {
        "now_anchor_iso", "knowledge_git_sha", "permitted_tables",
        "platform_context", "retry_context", "tool_calls_log", "explain_plan",
    }
    missing = required - field_names
    assert not missing, f"FeedbackRecord missing reproducibility fields: {missing}"


def test_build_feedback_record_captures_reproducibility_fields():
    msg = {"key": "msg_abc", "content": "answer text", "trace": [], "token_usage": {}}
    session_state = {
        "session_id": "sess_xyz", "user_identity": "alice", "user_role": "MA",
        "conversation_history": [{"query": "prev", "summary": "x"}],
    }
    pipeline_result = {
        "user_query": "q", "resolved_query": "q",
        "needs_clarification": False, "clarification_question": "",
        "glossary_matches": [{"term": "arbitrator"}],
        "relevant_tables": ["case", "user"],
        "schema_context": "=== VERIFIED COLUMNS ...",
        "generated_sql": "SELECT ...", "validated_sql": "SELECT ...",
        "assumptions": ["..."], "query_result": [{"name": "Edwin"}],
        "row_count": 1, "response_format": "table",
        "query_explanation": "...", "proactive_suggestions": ["a", "b"],
        "ambiguity_score": 0.0, "retry_count": 0, "is_feedback_retry": False,
        "now_anchor_iso": "2026-05-21T00:00:00+00:00",
        "knowledge_git_sha": "abc123",
        "permitted_tables": ["case", "user"],
        "platform_context": "[arbitrator_team_filter] ...",
        "retry_context": "",
        "tool_calls_log": [{"tool": "verify_sql_executes", "args": {}, "result_length": 100}],
        "explain_plan": {"total_rows": 100, "full_scan_tables": [], "warning": False},
    }
    record = fs.build_feedback_record(
        msg=msg, session_state=session_state, pipeline_result=pipeline_result,
        attestation=False, notes="wrong", error_categories=["wrong table"],
    )
    d = asdict(record)
    assert d["now_anchor_iso"] == "2026-05-21T00:00:00+00:00"
    assert d["knowledge_git_sha"] == "abc123"
    assert d["permitted_tables"] == ["case", "user"]
    assert "arbitrator_team_filter" in d["platform_context"]
    assert d["retry_context"] == ""
    assert d["tool_calls_log"][0]["tool"] == "verify_sql_executes"
    assert d["explain_plan"]["total_rows"] == 100


def test_build_feedback_record_safe_defaults():
    msg = {"key": "k", "content": "c", "trace": [], "token_usage": {}}
    session_state = {"session_id": "s", "user_identity": "", "user_role": "VO",
                     "conversation_history": []}
    pipeline_result = {
        "user_query": "q", "resolved_query": "",
        "glossary_matches": [], "relevant_tables": [],
        "schema_context": "", "generated_sql": "", "validated_sql": "",
        "assumptions": [], "query_result": [], "row_count": 0,
        "ambiguity_score": 0.0, "retry_count": 0,
        "is_feedback_retry": False, "needs_clarification": False,
    }
    record = fs.build_feedback_record(
        msg=msg, session_state=session_state, pipeline_result=pipeline_result,
        attestation=True, notes="", error_categories=[],
    )
    d = asdict(record)
    assert d["now_anchor_iso"] == ""
    assert d["knowledge_git_sha"] == ""
    assert d["permitted_tables"] == []
    assert d["platform_context"] == ""
    assert d["retry_context"] == ""
    assert d["tool_calls_log"] == []
    assert d["explain_plan"] == {}
