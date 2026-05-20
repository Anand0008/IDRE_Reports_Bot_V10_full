"""V10 single-call entrypoint (derived-path only).

Used by BOTH the Streamlit UI (`app.py`) and the smoke/harness runner.
Thin wrapper around `core.orchestrator.run_query` whose role is:

  1. Load `.env` at import time (Streamlit also does this; the smoke
     runner doesn't go through app.py and needs the explicit load).
  2. Open the OTEL `v10.query` root span (parent of all 14 agent spans
     emitted by the orchestrator's @trace_agent decorators).
  3. Forward every kwarg app.py + the smoke runner pass into the
     orchestrator unchanged, return the orchestrator's state dict
     unmodified.

History:
- Day 5: introduced as the harness wrapper around run_query.
- Node-Unify (2026-05-20): rewired to canonical derived shape so app.py
  could reach both known + derived endpoints through one entrypoint.
- Known-path-removal (2026-05-20): the known branch and all its helpers
  (router decision, IDRE HTTP client, response shaping, _coerce_rows,
  _extract_idre_meta, _format_idre_meta, _run_known_post_pipeline,
  _known_path_response, _canonical_empty_state) are gone. The wrapper
  reduces to env-load + root-span + orchestrator-forward.
  See local/docs/superpowers/specs/2026-05-20-known-path-removal-design.md
"""
import os
import sys
from pathlib import Path

HERE = Path(__file__).parent.resolve()
os.chdir(str(HERE))
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

# Load .env explicitly — Streamlit (app.py) already does this, but the
# harness / smoke runner imports this module directly and the orchestrator
# reads env vars (DB credentials, Gemini API key) at instantiation.
try:
    from dotenv import load_dotenv
    load_dotenv(HERE / ".env")
except ImportError:
    pass


def run_query_v10(
    prompt: str,
    now_anchor=None,
    user_role: str = "MA",
    feedback_correction_context: dict | None = None,
    is_feedback_retry: bool = False,
    # Kwargs needed by Streamlit (`app.py`)
    session_id: str = "harness",
    conversation_history: list | None = None,
    clarification_attempted: bool = False,
    user_identity: str = "",
) -> dict:
    """Single entrypoint. Forwards to the 14-agent derived orchestrator.

    Returns the orchestrator's state dict unmodified. The state includes
    every canonical key the UI renders: `formatted_response`,
    `query_result`, `row_count`, `validated_sql`, `agent_trace`,
    `chart_config`, `query_explanation`, `query_narrative`,
    `proactive_suggestions`, `assumptions`, `token_usage`,
    `slide_metadata`, `needs_clarification`, `clarification_question`,
    `error_message`.

    Args:
        prompt: user NL query
        now_anchor: optional temporal anchor for `:now` substitution.
            Defaults to datetime.now(UTC).
        user_role: V10 role code (MA, PA, PS, AC, AM, CB, VT, VO, DQD).
        feedback_correction_context, is_feedback_retry: Flow B (feedback
            retry) inputs; passed through to the orchestrator.
        session_id, conversation_history, clarification_attempted,
        user_identity: state-bag kwargs the Streamlit app passes through;
            forwarded into the orchestrator.
    """
    from tracing import get_tracer, redact
    tracer = get_tracer()
    with tracer.start_as_current_span("v10.query") as span:
        try:
            span.set_attribute("query.prompt", redact(prompt)[:500] if isinstance(prompt, str) else "")
            span.set_attribute("query.user_role", user_role)
        except Exception:
            pass

        return _run_derived(
            prompt, user_role,
            now_anchor=now_anchor,
            feedback_correction_context=feedback_correction_context,
            is_feedback_retry=is_feedback_retry,
            session_id=session_id,
            conversation_history=conversation_history,
            clarification_attempted=clarification_attempted,
            user_identity=user_identity,
        )


def _run_derived(
    prompt: str,
    user_role: str,
    now_anchor=None,
    feedback_correction_context: dict | None = None,
    is_feedback_retry: bool = False,
    session_id: str = "harness",
    conversation_history: list | None = None,
    clarification_attempted: bool = False,
    user_identity: str = "",
) -> dict:
    """Call the orchestrator inside a `v10.derived.orchestrator` child span.

    The child span captures `derived.row_count` + `derived.has_sql`
    attributes for downstream observability without bleeding LangGraph
    details into the parent span.
    """
    from tracing import get_tracer
    tracer = get_tracer()
    with tracer.start_as_current_span("v10.derived.orchestrator") as span:
        from core.orchestrator import run_query
        state = run_query(
            user_query=prompt,
            session_id=session_id or "harness",
            conversation_history=conversation_history or [],
            clarification_attempted=clarification_attempted,
            user_role=user_role,
            user_identity=user_identity or "",
            now_anchor=now_anchor,
            feedback_correction_context=feedback_correction_context,
            is_feedback_retry=is_feedback_retry,
        )
        try:
            span.set_attribute("derived.row_count", state.get("row_count", 0))
            span.set_attribute(
                "derived.has_sql",
                bool(state.get("validated_sql") or state.get("generated_sql")),
            )
        except Exception:
            pass
    return state


def run(prompt: str, user_role: str = "MA") -> dict:
    """Backward-compat alias for V8-style callers."""
    return run_query_v10(prompt, user_role=user_role)


if __name__ == "__main__":
    import json
    r = run_query_v10(sys.argv[1] if len(sys.argv) > 1 else "show me 5 most recent cases")
    print(json.dumps({
        "row_count": r.get("row_count"),
        "sql_len": len(r.get("validated_sql") or r.get("generated_sql") or ""),
        "trace_entries": len(r.get("agent_trace") or []),
        "formatted_preview": (r.get("formatted_response") or "")[:200],
    }, indent=2, default=str))
