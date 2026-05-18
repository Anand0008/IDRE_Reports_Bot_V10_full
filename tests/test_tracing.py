"""Unit tests for tracing helper module."""
import os
import pytest


def test_redact_email():
    from tracing import redact
    assert redact("contact <email> today") == "contact <email> today"


def test_redact_phone():
    from tracing import redact
    assert redact("call 5551234567 now") == "call <phone> now"


def test_redact_ssn():
    from tracing import redact
    assert redact("ssn 123-45-6789") == "ssn <ssn>"


def test_redact_keeps_other_text():
    from tracing import redact
    assert redact("hello world") == "hello world"


def test_redact_handles_non_string():
    from tracing import redact
    assert redact(None) is None
    assert redact(42) == 42


def test_trace_agent_no_op_when_disabled(monkeypatch):
    """When V10_OTEL_ENABLED=0, decorator must be identity (zero overhead)."""
    monkeypatch.setenv("V10_OTEL_ENABLED", "0")
    import importlib, sys
    if "tracing" in sys.modules:
        del sys.modules["tracing"]
    import tracing
    @tracing.trace_agent("v10.test.noop")
    def my_fn(state):
        return {**state, "ran": True}
    result = my_fn({"x": 1})
    assert result == {"x": 1, "ran": True}


def test_trace_agent_runs_when_enabled(monkeypatch):
    """When enabled, decorator runs the function and sets span attributes without raising."""
    monkeypatch.setenv("V10_OTEL_ENABLED", "1")
    import importlib, sys
    if "tracing" in sys.modules:
        del sys.modules["tracing"]
    import tracing
    @tracing.trace_agent("v10.test.enabled")
    def my_fn(state):
        return {**state, "ran": True, "agent_trace": [{"agent": "test", "status": "ok", "summary": "did the thing"}]}
    result = my_fn({"x": 1})
    assert result["ran"] is True


def test_traced_tool_call_no_op_when_disabled(monkeypatch):
    monkeypatch.setenv("V10_OTEL_ENABLED", "0")
    import importlib, sys
    if "tracing" in sys.modules:
        del sys.modules["tracing"]
    import tracing
    with tracing.traced_tool_call("test_tool"):
        pass  # just must not raise
