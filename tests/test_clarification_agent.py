"""Unit tests for clarification_agent_node (Node 3b)."""
from __future__ import annotations
import sys
from pathlib import Path
from unittest.mock import patch

_BOT_ROOT = Path(__file__).parent.parent.resolve()
if str(_BOT_ROOT) not in sys.path:
    sys.path.insert(0, str(_BOT_ROOT))

from agents.clarification_agent import (  # noqa: E402
    clarification_agent_node,
    _terminal_status_filter,
    _LLM_FAILURE_USER_MESSAGE,
)


# ── fast paths: nothing ambiguous, threshold not exceeded ────────────────────


def test_no_flags_proceeds():
    state = {
        "user_query": "how many cases are open?",
        "resolved_query": "how many cases are open?",
        "ambiguity_score": 0.0,
        "ambiguity_flags": [],
        "agent_trace": [],
    }
    result = clarification_agent_node(state)
    assert result["needs_clarification"] is False
    assert result["clarification_question"] == ""


def test_score_below_threshold_proceeds():
    state = {
        "user_query": "show me cases",
        "resolved_query": "show me cases",
        "ambiguity_score": 0.10,
        "ambiguity_flags": ["broad_entity"],
        "agent_trace": [],
    }
    result = clarification_agent_node(state)
    assert result["needs_clarification"] is False


def test_already_clarified_proceeds():
    state = {
        "user_query": "show me cases",
        "resolved_query": "show me cases",
        "ambiguity_score": 0.9,
        "ambiguity_flags": ["broad_entity", "vague_time"],
        "clarification_attempted": True,
        "agent_trace": [],
    }
    result = clarification_agent_node(state)
    assert result["needs_clarification"] is False


def test_env_threshold_disables_gate(monkeypatch):
    """V10_AMBIGUITY_THRESHOLD=1.0 → gate disabled even with flags."""
    monkeypatch.setenv("V10_AMBIGUITY_THRESHOLD", "1.0")
    state = {
        "user_query": "show recent stuff",
        "resolved_query": "show recent stuff",
        "ambiguity_score": 0.5,
        "ambiguity_flags": ["vague_time", "broad_entity"],
        "agent_trace": [],
    }
    result = clarification_agent_node(state)
    assert result["needs_clarification"] is False


# ── slow path: LLM is invoked ────────────────────────────────────────────────


def test_llm_called_when_threshold_exceeded():
    state = {
        "user_query": "show recent stuff",
        "resolved_query": "show recent stuff",
        "ambiguity_score": 0.6,
        "ambiguity_flags": ["vague_time", "broad_entity"],
        "agent_trace": [],
    }
    with patch(
        "agents.clarification_agent._generate_clarification",
        return_value=("Did you mean last 7 days or this month?", {"input": 10, "output": 5, "total": 15}),
    ):
        result = clarification_agent_node(state)
    assert result["needs_clarification"] is True
    assert "last 7 days" in result["clarification_question"]


def test_llm_failure_halts_pipeline():
    """When _generate_clarification returns None, node sets error_message
    + formatted_response so _route_after_clarification halts."""
    state = {
        "user_query": "show recent stuff",
        "resolved_query": "show recent stuff",
        "ambiguity_score": 0.6,
        "ambiguity_flags": ["vague_time", "broad_entity"],
        "agent_trace": [],
    }
    with patch(
        "agents.clarification_agent._generate_clarification",
        return_value=None,
    ):
        result = clarification_agent_node(state)
    assert result["error_message"] == "clarification_llm_failure"
    assert result["formatted_response"] == _LLM_FAILURE_USER_MESSAGE
    assert result["needs_clarification"] is False
    assert any(
        t.get("agent") == "Clarification Agent" and t.get("status") == "error"
        for t in result["agent_trace"]
    )


# ── glossary-sourced terminal-status filter ──────────────────────────────────


def test_terminal_status_filter_from_glossary():
    """Single source of truth — must return a usable SQL fragment."""
    f = _terminal_status_filter()
    # Non-empty and references the case.status column
    assert f
    assert "status" in f.lower()


# ── V10 cleanup regressions ──────────────────────────────────────────────────


def test_auto_answer_feature_removed():
    """The structurally-broken V6 auto-answer feature is gone for good."""
    from agents import clarification_agent as ca_mod
    assert not hasattr(ca_mod, "_find_auto_answer")
    assert not hasattr(ca_mod, "_save_clarification_answer")
    assert not hasattr(ca_mod, "_load_clarification_history")
    assert not hasattr(ca_mod, "_CLARIFICATION_HISTORY_PATH")
