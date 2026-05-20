"""Tests for harness_entrypoint.py (derived-only wrapper).

Pins the three surviving behaviors of run_query_v10: kwarg forwarding
to core.orchestrator.run_query, state passthrough, and the OTEL
v10.query root span. The known path was removed 2026-05-20 — see
local/docs/superpowers/specs/2026-05-20-known-path-removal-design.md.
"""
import inspect

import harness_entrypoint as he


# ── Signature must accept everything app.py passes ───────────────────


def test_run_query_v10_signature_accepts_app_kwargs():
    """app.py passes prompt, session_id, conversation_history,
    clarification_attempted, user_role, user_identity,
    feedback_correction_context, is_feedback_retry. All must be in the
    signature."""
    sig = inspect.signature(he.run_query_v10)
    for name in (
        "prompt", "now_anchor", "user_role",
        "feedback_correction_context", "is_feedback_retry",
        "session_id", "conversation_history", "clarification_attempted",
        "user_identity",
    ):
        assert name in sig.parameters, f"signature missing required kwarg: {name}"


# ── Characterization tests for run_query_v10 wrapper ─────────────────


def _make_derived_prompt():
    """A prompt the orchestrator will accept; doesn't matter what
    classify-shape it has now that the router is gone."""
    return "compare ip and nip win rates by region across the last quarter"


def test_run_query_v10_forwards_all_kwargs_to_orchestrator(monkeypatch):
    """Every kwarg app.py passes to run_query_v10 must reach
    core.orchestrator.run_query unchanged."""
    captured = {}

    def fake_run_query(**kwargs):
        captured.update(kwargs)
        return {"formatted_response": "ok", "row_count": 0, "agent_trace": []}

    import core.orchestrator as orch
    monkeypatch.setattr(orch, "run_query", fake_run_query)

    he.run_query_v10(
        prompt=_make_derived_prompt(),
        user_role="MA",
        session_id="sess_abc",
        conversation_history=[{"query": "prev", "summary": "x"}],
        clarification_attempted=False,
        user_identity="alice",
        feedback_correction_context=None,
        is_feedback_retry=False,
    )

    assert captured["user_query"] == _make_derived_prompt()
    assert captured["user_role"] == "MA"
    assert captured["session_id"] == "sess_abc"
    assert captured["conversation_history"] == [{"query": "prev", "summary": "x"}]
    assert captured["clarification_attempted"] is False
    assert captured["user_identity"] == "alice"
    assert captured["feedback_correction_context"] is None
    assert captured["is_feedback_retry"] is False


def test_run_query_v10_returns_orchestrator_state_unchanged(monkeypatch):
    """run_query_v10 must return the orchestrator's state dict
    identity-equal — no wrapping, no key augmentation."""
    sentinel = {"_marker": "from-orchestrator", "formatted_response": "x",
                "row_count": 0, "agent_trace": []}

    def fake_run_query(**kwargs):
        return sentinel

    import core.orchestrator as orch
    monkeypatch.setattr(orch, "run_query", fake_run_query)

    result = he.run_query_v10(prompt=_make_derived_prompt(), user_role="MA")
    # Identity-equal: the harness must not re-shape or augment.
    assert result is sentinel


def test_run_query_v10_opens_v10_query_otel_span(monkeypatch):
    """The OTEL v10.query root span must be emitted on every invocation
    (observability invariant per [[feedback-observability]])."""
    span_names: list[str] = []

    import tracing
    real_tracer = tracing.get_tracer()
    original_start = real_tracer.start_as_current_span

    class _RecordingTracer:
        def start_as_current_span(self, name, *args, **kwargs):
            span_names.append(name)
            return original_start(name, *args, **kwargs)

    monkeypatch.setattr(tracing, "get_tracer", lambda: _RecordingTracer())

    import core.orchestrator as orch
    monkeypatch.setattr(orch, "run_query",
                        lambda **kwargs: {"formatted_response": "ok",
                                          "row_count": 0,
                                          "agent_trace": []})

    he.run_query_v10(prompt=_make_derived_prompt(), user_role="MA")

    assert "v10.query" in span_names, \
        f"v10.query span not emitted; got: {span_names}"


# ── Public-surface pin ───────────────────────────────────────────────


def test_public_surface_preserved():
    """The module-level symbols app.py + smoke runner depend on must
    remain exported."""
    for name in ("run_query_v10", "_run_derived", "run"):
        assert hasattr(he, name), f"public surface lost: {name}"
    # Symbols that USED to exist but were removed in the known-path
    # rip-out — assert they are gone, so accidental re-introduction
    # fails the suite.
    for name in (
        "_known_path_response", "_coerce_rows", "_extract_idre_meta",
        "_format_idre_meta", "_run_known_post_pipeline",
        "_canonical_empty_state", "_render_known_markdown",
    ):
        assert not hasattr(he, name), (
            f"removed symbol re-appeared: {name} — known-path code drifted back in"
        )
