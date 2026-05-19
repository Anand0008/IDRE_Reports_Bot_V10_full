"""Unit tests for context_loader_node and its supporting functions.

Locks the Node-2 deepdive behavior (2026-05-19): grammatical registry
substitutions, tightened reference pattern, status-filter regex sourced
from enum_catalog, LLM-failure halt path.
"""
from __future__ import annotations
import sys
from pathlib import Path
from unittest.mock import patch

_BOT_ROOT = Path(__file__).parent.parent.resolve()
if str(_BOT_ROOT) not in sys.path:
    sys.path.insert(0, str(_BOT_ROOT))

from agents.context_loader import (  # noqa: E402
    _needs_resolution,
    _resolve_from_registry,
    _extract_entities,
    _build_status_filter_pattern,
    context_loader_node,
    _LLM_FAILURE_USER_MESSAGE,
)


# ── _needs_resolution: tightened pattern ─────────────────────────────────────


def test_needs_resolution_no_history():
    """Empty history → never needs resolution regardless of query content."""
    assert not _needs_resolution("show me those cases", [])
    assert not _needs_resolution("what about them?", [])


def test_needs_resolution_self_contained_with_history():
    """Standalone 'this' / 'that' / 'same' must NOT trigger LLM resolution."""
    history = [{"query": "earlier", "summary": "..."}]
    # 'this' in 'this month' → was a false trigger pre-fix
    assert not _needs_resolution("show me this month's revenue", history)
    # 'that' in 'that arbitrator' → was a false trigger pre-fix
    assert not _needs_resolution("give me cases that closed last week", history)
    # 'same' standalone → was a false trigger pre-fix
    assert not _needs_resolution("the same arbitrator twice in a week", history)


def test_needs_resolution_anaphora_with_history():
    """Multi-word anaphora still triggers resolution."""
    history = [{"query": "show pending RFI cases", "summary": "382 cases"}]
    assert _needs_resolution("show me those cases by org", history)
    assert _needs_resolution("what about them?", history)
    assert _needs_resolution("same filter but for last month", history)
    assert _needs_resolution("and how many of them are batched?", history)


# ── _resolve_from_registry: grammatical substitution ─────────────────────────


def test_resolve_registry_those_cases_wraps_status():
    """'those cases' must resolve to 'cases with status X', not raw enum."""
    registry = {"status_filter": "PENDING_RFI"}
    out, changed = _resolve_from_registry("show me payment totals for those cases", registry)
    assert changed
    assert "cases with status PENDING_RFI" in out
    # Critical: raw enum value should never be left dangling
    assert "for PENDING_RFI" not in out


def test_resolve_registry_that_org_wraps():
    registry = {"org_name": "HaloMD"}
    out, changed = _resolve_from_registry("show me cases for that organization", registry)
    assert changed
    assert "organization HaloMD" in out


def test_resolve_registry_same_time_passthrough():
    """'same time' resolves to the raw time_range value (it's already a phrase)."""
    registry = {"time_range": "last month"}
    out, changed = _resolve_from_registry("how many cases in same time period", registry)
    assert changed
    assert "last month" in out


def test_resolve_registry_no_matching_key():
    """Pattern matches but no registry value → no change."""
    registry = {"org_name": "HaloMD"}  # no status_filter
    out, changed = _resolve_from_registry("show me those cases", registry)
    assert not changed
    assert out == "show me those cases"


def test_resolve_registry_empty():
    out, changed = _resolve_from_registry("show me those cases", {})
    assert not changed
    assert out == "show me those cases"


# ── _extract_entities: enum catalog drives status_filter ─────────────────────


def test_status_pattern_includes_missing_enums():
    """All CaseStatus enum values from enum_catalog should be matchable."""
    pat = _build_status_filter_pattern()
    # The ones the pre-fix regex was missing:
    for status in [
        "CLOSED_DEFAULT_IP", "CLOSED_DEFAULT_NIP",
        "FINAL_DETERMINATION_PENDING", "FINAL_ELIGIBILITY_COMPLETED",
        "INELIGIBLE_PENDING_ADMIN_FEE", "PENDING_INITIAL_RFI",
        "PENDING_CLOSURE_PAYMENTS", "PENDING_ADMINISTRATIVE_CLOSURE",
        "NOTICE_OF_DISMISSAL_NON_PAYMENT",
    ]:
        assert pat.search(f"how many cases are {status}?"), (
            f"status_filter regex missed {status}"
        )


def test_status_pattern_keeps_aliases():
    """Colloquial aliases still match alongside the enum values."""
    pat = _build_status_filter_pattern()
    for alias in ["open", "closed", "pending", "ineligible", "active", "terminal"]:
        assert pat.search(f"show me {alias} cases")


def test_extract_entities_finds_new_status():
    ents = _extract_entities("how many cases are in FINAL_DETERMINATION_PENDING?")
    assert ents.get("status_filter", "").upper() == "FINAL_DETERMINATION_PENDING"


def test_extract_entities_time_range():
    ents = _extract_entities("show me cases from last month")
    assert "time_range" in ents
    assert "month" in ents["time_range"].lower()


def test_extract_entities_org_name():
    ents = _extract_entities("show me Capitol Bridge payouts")
    assert ents.get("org_name", "").lower() == "capitol bridge"


# ── context_loader_node: LLM failure halt path ───────────────────────────────


def test_context_loader_llm_failure_halts_pipeline():
    """When _resolve_query returns None, node sets error_message +
    formatted_response, allowing _route_after_context_loader to halt."""
    state = {
        "user_query": "what about those cases?",
        "conversation_history": [{"query": "show me PENDING_RFI cases", "summary": "382"}],
        "user_role": "MA",
        "entity_registry": {},  # empty, so registry resolution fails over to LLM
        "agent_trace": [],
        "token_usage": {},
    }
    # Mock the LLM resolver to fail
    with patch("agents.context_loader._resolve_query", return_value=None):
        result = context_loader_node(state)
    assert result["error_message"] == "context_loader_llm_failure"
    assert result["formatted_response"] == _LLM_FAILURE_USER_MESSAGE
    # Permissions should still be populated despite the early return
    assert "permitted_tables" in result
    # Trace records the failure
    assert any(
        t.get("agent") == "Context Loader" and t.get("status") == "error"
        for t in result["agent_trace"]
    )


def test_context_loader_no_resolution_needed_succeeds():
    """Self-contained query with empty history → no LLM call → normal output."""
    state = {
        "user_query": "how many cases are open?",
        "conversation_history": [],
        "user_role": "MA",
        "entity_registry": {},
        "agent_trace": [],
        "token_usage": {},
    }
    # The LLM should not be invoked for a self-contained query
    with patch("agents.context_loader._resolve_query") as mock_llm:
        result = context_loader_node(state)
        assert not mock_llm.called
    assert "error_message" not in result or not result.get("error_message")
    assert result["resolved_query"] == "how many cases are open?"
    assert result["user_role"] == "MA"


def test_context_loader_unknown_role_falls_back():
    """Unknown role → permissions module falls back to default_role (VO)."""
    state = {
        "user_query": "how many cases are open?",
        "conversation_history": [],
        "user_role": "ZZ",  # invalid
        "entity_registry": {},
        "agent_trace": [],
        "token_usage": {},
    }
    result = context_loader_node(state)
    # Should still have permitted_tables set (the VO defaults)
    assert result["permitted_tables"]
    # The role string is preserved as passed
    assert result["user_role"] == "ZZ"
