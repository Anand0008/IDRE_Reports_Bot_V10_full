"""Tests for utils/audit_writer.py — verify the 2026-05-21 reproducibility
enrichment captures every relevant state field into AuditEvent."""
import json
import os
import time
from dataclasses import fields

from utils import audit_writer as aw


def test_audit_event_has_all_reproducibility_fields():
    """Pin that every new reproducibility field is declared on AuditEvent."""
    field_names = {f.name for f in fields(aw.AuditEvent)}
    required = {
        "now_anchor_iso", "knowledge_git_sha", "permitted_tables",
        "schema_context", "platform_context", "retry_context",
        "tool_calls_log", "explain_plan", "assumptions", "agent_trace",
        "conversation_history", "query_result_head",
    }
    missing = required - field_names
    assert not missing, f"AuditEvent missing reproducibility fields: {missing}"


def _wait_for_audit_write(path, timeout=1.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return True
        time.sleep(0.02)
    return False


def _full_state():
    return {
        "session_id": "sess_abc", "user_role": "MA", "user_identity": "alice",
        "user_query": "list arbitrators with most active cases",
        "resolved_query": "list arbitrators with most active cases",
        "ambiguity_score": 0.0,
        "glossary_matches": [{"term": "arbitrator"}],
        "relevant_tables": ["case", "user"],
        "generated_sql": "SELECT u.name, COUNT(c.id) FROM ...",
        "validated_sql": "SELECT u.name, COUNT(c.id) FROM ...",
        "execution_status": "success", "execution_error": None, "error_message": "",
        "retry_count": 0, "row_count": 57, "response_format": "table",
        "pipeline_start_ms": 1716200000000,
        "agent_timings": {},
        "agent_trace": [
            {"agent": "Context Loader", "status": "ok", "summary": "...", "detail": []},
            {"agent": "SQL Writer", "status": "ok", "summary": "...", "detail": []},
        ],
        "needs_clarification": False, "permission_violation": False,
        "assumptions": ["Arbitrators are users with role IN (...)"],
        "is_feedback_retry": False, "feedback_record_id": "",
        "token_usage": {"SQL Writer": {"input": 100, "output": 50, "total": 150}},
        "query_complexity": {"total_rows": 100000, "join_count": 1, "expensive": False},
        "self_verification": {}, "entity_registry": {"status_filter": "active"},
        "debug_error_type": "", "debug_retry_strategy": "",
        "now_anchor_iso": "2026-05-21T00:00:00+00:00",
        "knowledge_git_sha": "abc123def456",
        "permitted_tables": ["case", "user", "payment"],
        "schema_context": "=== VERIFIED COLUMNS: case === ...",
        "platform_context": "[arbitrator_team_filter] ... [terminal_statuses] ...",
        "retry_context": "",
        "tool_calls_log": [{"tool": "verify_sql_executes", "args": {"sql": "..."}, "result_length": 120}],
        "explain_plan": {"total_rows": 100, "full_scan_tables": [], "warning": False},
        "conversation_history": [{"query": "prev", "summary": "x"}],
        "query_result": [{"name": "Edwin", "count": 1040}, {"name": "Lucinda", "count": 981}],
    }


def test_audit_event_captures_full_reproducibility_context(tmp_path, monkeypatch):
    audit_path = str(tmp_path / "audit_log.jsonl")
    monkeypatch.setattr(aw, "AUDIT_LOG_PATH", audit_path)
    aw.build_and_log(_full_state(), total_ms=1234)
    assert _wait_for_audit_write(audit_path), "audit log not written"

    with open(audit_path, encoding="utf-8") as f:
        record = json.loads(f.readline())

    assert record["now_anchor_iso"] == "2026-05-21T00:00:00+00:00"
    assert record["knowledge_git_sha"] == "abc123def456"
    assert record["permitted_tables"] == ["case", "user", "payment"]
    assert "VERIFIED COLUMNS" in record["schema_context"]
    assert "arbitrator_team_filter" in record["platform_context"]
    assert record["retry_context"] == ""
    assert record["tool_calls_log"][0]["tool"] == "verify_sql_executes"
    assert record["explain_plan"]["total_rows"] == 100
    assert record["assumptions"] == ["Arbitrators are users with role IN (...)"]
    assert len(record["agent_trace"]) == 2
    assert record["conversation_history"] == [{"query": "prev", "summary": "x"}]
    assert record["query_result_head"] == [
        {"name": "Edwin", "count": 1040},
        {"name": "Lucinda", "count": 981},
    ]


def test_audit_event_safe_defaults_on_halted_state(tmp_path, monkeypatch):
    """A halt-at-context-loader run has no schema/platform/tools data —
    must default to safe empty types, not crash."""
    audit_path = str(tmp_path / "audit_log.jsonl")
    monkeypatch.setattr(aw, "AUDIT_LOG_PATH", audit_path)
    halted = {
        "session_id": "sess_x", "user_role": "MA", "user_identity": "bob",
        "user_query": "ambiguous question", "resolved_query": "",
        "error_message": "context_loader_llm_failure", "execution_error": None,
        "execution_status": "validation_failed",
        "agent_trace": [{"agent": "Context Loader", "status": "error", "summary": "Resolver failed", "detail": []}],
        "needs_clarification": False, "row_count": 0, "retry_count": 0,
        "pipeline_start_ms": 1716200000000,
    }
    aw.build_and_log(halted, total_ms=200)
    assert _wait_for_audit_write(audit_path)
    with open(audit_path, encoding="utf-8") as f:
        record = json.loads(f.readline())
    assert record["schema_context"] == ""
    assert record["platform_context"] == ""
    assert record["retry_context"] == ""
    assert record["tool_calls_log"] == []
    assert record["explain_plan"] == {}
    assert record["assumptions"] == []
    assert record["query_result_head"] == []
    assert record["permitted_tables"] == []
    assert record["now_anchor_iso"] == ""
    assert record["knowledge_git_sha"] == ""
    assert record["conversation_history"] == []


def test_audit_event_query_result_head_trims_to_5(tmp_path, monkeypatch):
    audit_path = str(tmp_path / "audit_log.jsonl")
    monkeypatch.setattr(aw, "AUDIT_LOG_PATH", audit_path)
    state = _full_state()
    state["query_result"] = [{"i": i} for i in range(50)]
    aw.build_and_log(state, total_ms=100)
    assert _wait_for_audit_write(audit_path)
    with open(audit_path, encoding="utf-8") as f:
        record = json.loads(f.readline())
    assert len(record["query_result_head"]) == 5
    assert record["query_result_head"][0] == {"i": 0}
    assert record["query_result_head"][4] == {"i": 4}
