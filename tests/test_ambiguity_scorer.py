"""Unit tests for ambiguity_scorer_node and the shared threshold helper."""
from __future__ import annotations
import sys
from pathlib import Path

_BOT_ROOT = Path(__file__).parent.parent.resolve()
if str(_BOT_ROOT) not in sys.path:
    sys.path.insert(0, str(_BOT_ROOT))

from agents.ambiguity_scorer import score_ambiguity, ambiguity_scorer_node  # noqa: E402
from utils.ambiguity_threshold import resolve_threshold, DEFAULT_THRESHOLD  # noqa: E402


# ── threshold helper ─────────────────────────────────────────────────────────


def test_threshold_default():
    assert resolve_threshold(None) == DEFAULT_THRESHOLD
    assert resolve_threshold({}) == DEFAULT_THRESHOLD


def test_threshold_user_pref_override():
    assert resolve_threshold({"ambiguity_threshold": 0.5}) == 0.5


def test_threshold_env_override_wins(monkeypatch):
    monkeypatch.setenv("V10_AMBIGUITY_THRESHOLD", "1.0")
    assert resolve_threshold({"ambiguity_threshold": 0.5}) == 1.0


def test_threshold_env_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("V10_AMBIGUITY_THRESHOLD", "not-a-float")
    assert resolve_threshold(None) == DEFAULT_THRESHOLD


# ── score_ambiguity flag triggers ────────────────────────────────────────────


def test_score_no_flags_on_self_contained():
    score, flags = score_ambiguity("how many cases are open?", [], glossary_matches=[])
    assert score == 0.0
    assert flags == []


def test_vague_time_fires_on_recent():
    # Note: queries containing 'cases'/'disputes'/'payments' suppress
    # vague_time via _DEFAULT_RESOLUTIONS — using neutral terms here.
    score, flags = score_ambiguity("show me recent activity", [], glossary_matches=[])
    assert "vague_time" in flags
    assert score > 0


def test_vague_time_suppressed_by_specific_term():
    """'this month' is a specific term and must NOT trigger vague_time."""
    score, flags = score_ambiguity(
        "how many cases this month", [], glossary_matches=[]
    )
    assert "vague_time" not in flags


def test_unresolved_pronoun_fires_without_history():
    score, flags = score_ambiguity("show me them", [], glossary_matches=[])
    assert "unresolved_pronoun" in flags


def test_unresolved_pronoun_suppressed_with_history():
    score, flags = score_ambiguity(
        "show me them",
        [{"query": "pending cases", "summary": "..."}],
        glossary_matches=[],
    )
    assert "unresolved_pronoun" not in flags


# ── node-level: env override disables gate ───────────────────────────────────


def test_node_env_override_zero_threshold_keeps_flags(monkeypatch):
    """Threshold override changes the threshold; flags still get computed."""
    monkeypatch.setenv("V10_AMBIGUITY_THRESHOLD", "1.0")
    state = {
        "user_query": "show me recent activity",
        "conversation_history": [],
        "glossary_matches": [],
        "user_preferences": {},
        "agent_trace": [],
    }
    result = ambiguity_scorer_node(state)
    # Flags are computed regardless of threshold — threshold is only
    # consulted by the clarification_agent.
    assert "vague_time" in result["ambiguity_flags"]


def test_node_no_calibration_log_written(tmp_path, monkeypatch):
    """V10 cleanup regression: ambiguity_calibration.jsonl must NOT be
    written. Pre-cleanup code appended to it on every call."""
    # Point _DATA_DIR-equivalent paths at tmp; since the calibration log
    # writer is removed, no file should ever appear.
    state = {
        "user_query": "show me anything",
        "conversation_history": [],
        "glossary_matches": [],
        "user_preferences": {},
        "agent_trace": [],
    }
    ambiguity_scorer_node(state)
    # Confirm we don't have ambiguity_calibration logic anywhere
    from agents import ambiguity_scorer as scorer_mod
    assert not hasattr(scorer_mod, "_log_calibration")
    assert not hasattr(scorer_mod, "_CALIBRATION_LOG")
