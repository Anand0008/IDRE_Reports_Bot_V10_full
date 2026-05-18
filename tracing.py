"""OTEL tracing helper for V10 bot.

- @trace_agent(name): decorator wrapping agent *_node functions in OTEL spans.
- traced_tool_call(name): context manager wrapping SQL Writer tool dispatch.
- redact(text): strip emails/phones/SSNs from free-text span attributes.

When env V10_OTEL_ENABLED is "0"/"false"/"no", decorators are zero-cost no-ops.
"""
import os
import re
from contextlib import contextmanager
from functools import wraps

_OTEL_ENABLED = os.environ.get("V10_OTEL_ENABLED", "1").lower() in ("1", "true", "yes")

if _OTEL_ENABLED:
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource, SERVICE_NAME

    # Only install provider if a default NoOp provider is present (avoid double-install)
    _current = trace.get_tracer_provider()
    if not isinstance(_current, TracerProvider):
        _provider = TracerProvider(resource=Resource.create({SERVICE_NAME: "v10-bot"}))
        _exporter = OTLPSpanExporter(endpoint="http://localhost:4318/v1/traces")
        _provider.add_span_processor(BatchSpanProcessor(_exporter))
        trace.set_tracer_provider(_provider)
    _tracer = trace.get_tracer("v10.bot")
else:
    _tracer = None


def redact(text):
    """Strip emails, phones, SSNs from free-text span attributes."""
    if not isinstance(text, str):
        return text
    text = re.sub(r'\b\d{3}-\d{2}-\d{4}\b', '<ssn>', text)
    text = re.sub(r'[\w.+-]+@[\w-]+\.[\w.-]+', '<email>', text)
    text = re.sub(r'\b\d{10,}\b', '<phone>', text)
    return text


def trace_agent(name):
    """Decorator for agent_node functions. Wraps in OTEL span (or identity if disabled)."""
    if not _OTEL_ENABLED:
        return lambda fn: fn  # zero-cost no-op
    def deco(fn):
        @wraps(fn)
        def wrapped(state):
            with _tracer.start_as_current_span(name) as span:
                try:
                    if isinstance(state, dict):
                        span.set_attribute("agent.input_keys", list(state.keys())[:50])
                    span.set_attribute("agent.name", name)
                except Exception:
                    pass
                result = fn(state)
                try:
                    if isinstance(result, dict) and isinstance(state, dict):
                        changed = [k for k in result if k not in state or result[k] != state.get(k)]
                        span.set_attribute("agent.output_keys", changed[:50])
                    if isinstance(result, dict) and "agent_trace" in result:
                        trace_list = result["agent_trace"]
                        if trace_list and isinstance(trace_list[-1], dict):
                            last = trace_list[-1]
                            if "status" in last:
                                span.set_attribute("agent.status", str(last["status"]))
                            if "summary" in last:
                                summary = last["summary"]
                                if isinstance(summary, str):
                                    span.set_attribute("agent.summary", redact(summary[:500]))
                except Exception:
                    pass
                return result
        return wrapped
    return deco


@contextmanager
def traced_tool_call(tool_name):
    """Context manager for SQL Writer tool calls."""
    if not _OTEL_ENABLED:
        yield None
        return
    with _tracer.start_as_current_span(f"v10.tool.{tool_name}") as span:
        try:
            span.set_attribute("tool.name", tool_name)
        except Exception:
            pass
        yield span


def get_tracer():
    """Return the V10 OTEL tracer (or a no-op tracer if disabled)."""
    if _OTEL_ENABLED:
        return _tracer
    # Return a no-op tracer that supports the start_as_current_span context manager
    from opentelemetry import trace as _trace
    return _trace.get_tracer("v10.bot.noop")
