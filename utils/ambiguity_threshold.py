"""Single source of truth for the ambiguity-clarification threshold — V10.

Both `agents/ambiguity_scorer.py` and `agents/clarification_agent.py` use
the threshold to decide whether to pause the pipeline for a clarifying
question. They MUST agree. Centralising the lookup here eliminates the
risk of the two agents reading the env var or user_preferences with
slightly different fallback logic.

Priority (highest first):
  1. `V10_AMBIGUITY_THRESHOLD` env override — used by automated tests
     that can't respond to clarifications. Setting this to ``1.0``
     effectively disables the clarification gate.
  2. `user_preferences['ambiguity_threshold']` — explicit per-session
     override from the Streamlit UI (not currently wired in V10).
  3. `DEFAULT_THRESHOLD` (0.30).
"""
from __future__ import annotations

import os
from typing import Optional

DEFAULT_THRESHOLD: float = 0.30


def resolve_threshold(user_preferences: Optional[dict] = None) -> float:
    """Return the effective ambiguity threshold.

    See module docstring for the priority order.
    """
    env = os.environ.get("V10_AMBIGUITY_THRESHOLD")
    if env is not None:
        try:
            return float(env)
        except ValueError:
            pass
    if user_preferences:
        explicit = user_preferences.get("ambiguity_threshold")
        if explicit is not None:
            try:
                return float(explicit)
            except (TypeError, ValueError):
                pass
    return DEFAULT_THRESHOLD
