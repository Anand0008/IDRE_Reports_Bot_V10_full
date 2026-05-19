"""Unit tests for feedback_injector_node (the Flow B handoff).

Verifies the correction block is prepended to retry_context, error
categories are logged, and the V10 cleanup (dropped metric_card
proposal) didn't break the node's main behavior.
"""
from __future__ import annotations
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

_BOT_ROOT = Path(__file__).parent.parent.resolve()
if str(_BOT_ROOT) not in sys.path:
    sys.path.insert(0, str(_BOT_ROOT))


def _state_with_correction(**overrides):
    state = {
        "user_query": "how many cases pending payment?",
        "feedback_correction_context": {
            "original_query": "how many cases pending payment?",
            "error_categories": ["wrong-table", "wrong-filter"],
            "free_text_note": "user said P=0 means no completed payments",
            "original_result_summary": "12 cases (was wrong)",
            "feedback_record_id": "fb-1234",
        },
        "is_feedback_retry": True,
        "agent_trace": [],
        "retry_context": "",
        "generated_sql": "",
        "validated_sql": "",
    }
    state.update(overrides)
    return state


def test_feedback_injector_noop_without_correction(tmp_path, monkeypatch):
    """If feedback_correction_context is missing, the node is a no-op."""
    monkeypatch.setattr(
        "agents.feedback_injector._DATA_DIR", str(tmp_path)
    )
    from agents.feedback_injector import feedback_injector_node
    state = {"user_query": "anything", "feedback_correction_context": None}
    result = feedback_injector_node(state)
    assert result is state  # literal no-op return


def test_feedback_injector_prepends_correction_block(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "agents.feedback_injector._DATA_DIR", str(tmp_path)
    )
    # Re-derive paths because the module caches them at import time
    monkeypatch.setattr(
        "agents.feedback_injector._ERROR_WEIGHTS_PATH",
        str(tmp_path / "error_category_weights.json"),
    )
    monkeypatch.setattr(
        "agents.feedback_injector._CORRECTION_LOG_PATH",
        str(tmp_path / "correction_success_log.json"),
    )
    from agents.feedback_injector import feedback_injector_node
    state = _state_with_correction(retry_context="prior context here")
    result = feedback_injector_node(state)
    assert "FEEDBACK CORRECTION" in result["retry_context"]
    assert "wrong-table" in result["retry_context"]
    assert "wrong-filter" in result["retry_context"]
    # Existing retry_context preserved after the correction block
    assert "prior context here" in result["retry_context"]


def test_feedback_injector_logs_correction(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "agents.feedback_injector._DATA_DIR", str(tmp_path)
    )
    log_path = tmp_path / "correction_success_log.json"
    monkeypatch.setattr(
        "agents.feedback_injector._CORRECTION_LOG_PATH", str(log_path)
    )
    monkeypatch.setattr(
        "agents.feedback_injector._ERROR_WEIGHTS_PATH",
        str(tmp_path / "error_category_weights.json"),
    )
    from agents.feedback_injector import feedback_injector_node
    state = _state_with_correction()
    feedback_injector_node(state)
    assert log_path.exists()
    data = json.loads(log_path.read_text())
    assert data["corrections"][-1]["pattern"] == "how many cases pending payment?"
    assert data["corrections"][-1]["categories"] == ["wrong-table", "wrong-filter"]


def test_feedback_injector_does_not_write_metric_card(tmp_path, monkeypatch):
    """V10 cleanup regression: ensure proposed_metric_cards.json is NEVER
    created (it was dead code in V6, removed in V10)."""
    monkeypatch.setattr(
        "agents.feedback_injector._DATA_DIR", str(tmp_path)
    )
    monkeypatch.setattr(
        "agents.feedback_injector._CORRECTION_LOG_PATH",
        str(tmp_path / "correction_success_log.json"),
    )
    monkeypatch.setattr(
        "agents.feedback_injector._ERROR_WEIGHTS_PATH",
        str(tmp_path / "error_category_weights.json"),
    )
    from agents.feedback_injector import feedback_injector_node
    # Repeat 5 times — pre-fix code would write proposed_metric_cards.json
    # on the 4th+ call (count >= 3 branch).
    state = _state_with_correction()
    for _ in range(5):
        feedback_injector_node(state.copy())
    # Confirm the dead-code file was never written
    assert not (tmp_path / "proposed_metric_cards.json").exists()


def test_feedback_injector_trace_lists_categories(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "agents.feedback_injector._DATA_DIR", str(tmp_path)
    )
    monkeypatch.setattr(
        "agents.feedback_injector._CORRECTION_LOG_PATH",
        str(tmp_path / "correction_success_log.json"),
    )
    monkeypatch.setattr(
        "agents.feedback_injector._ERROR_WEIGHTS_PATH",
        str(tmp_path / "error_category_weights.json"),
    )
    from agents.feedback_injector import feedback_injector_node
    state = _state_with_correction()
    result = feedback_injector_node(state)
    last_trace = result["agent_trace"][-1]
    assert last_trace["agent"] == "Feedback Injector"
    assert "wrong-table" in str(last_trace["detail"])
