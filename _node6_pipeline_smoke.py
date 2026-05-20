"""Derived-path pipeline smoke test — single-prompt end-to-end run.

NOT part of V10 production. Calls the harness entrypoint directly to
exercise the full derived-path pipeline (context_loader → ambiguity →
clarification → schema_mapper + platform_context → schema_verifier →
sql_writer (+ tools) → sql_validator → executor → post_processor →
output_formatter → response_formatter).

Hits real Gemini API + real RDS staging via .env. Read-only — the
pipeline is SELECT-only by design.

Run:
    py -3.11 _node6_pipeline_smoke.py
    py -3.11 _node6_pipeline_smoke.py "your custom prompt here"
    py -3.11 _node6_pipeline_smoke.py --batch
    py -3.11 _node6_pipeline_smoke.py --derived

History:
- 2026-05-20 known-path removal: stripped --known, --full, --regression
  modes + KNOWN_PATH_PROMPTS + REGRESSION_PROMPTS constants. Only
  derived prompts remain.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

# Force UTF-8 stdout so we can print ≤ ≥ → … bullets from agent traces
# without Windows cp1252 blowing up.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Silence the OTEL exporter's "connection refused" warnings — no
# collector is running locally by default. Spans are still emitted; we
# just drop the export attempts.
os.environ.setdefault("OTEL_SDK_DISABLED", "true")

HERE = Path(__file__).parent.resolve()
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))


DEFAULT_PROMPT = "show me 5 most recent cases"


def _fmt_block(title: str, body: str, max_lines: int = 40) -> str:
    """Print a labelled section with a max-line trim for very long fields."""
    lines = body.splitlines() if body else ["(empty)"]
    shown = lines[:max_lines]
    truncated = len(lines) > max_lines
    out = [f"\n{'=' * 78}", f"  {title}", "=" * 78]
    out.extend(shown)
    if truncated:
        out.append(f"... ({len(lines) - max_lines} more lines)")
    return "\n".join(out)


def _summarize_agent_trace(trace: list[dict]) -> str:
    """One-line-per-agent summary of the trace."""
    if not trace:
        return "(no trace entries)"
    rows = []
    for i, e in enumerate(trace, 1):
        agent = e.get("agent", "?")
        status = e.get("status", "?")
        summary = (e.get("summary") or "").splitlines()[0][:120]
        rows.append(f"{i:2}. [{status:>4}] {agent:<22} {summary}")
        detail = e.get("detail") or []
        for d in detail[:3]:
            d_str = str(d)
            if len(d_str) > 110:
                d_str = d_str[:107] + "..."
            rows.append(f"        · {d_str}")
        if len(detail) > 3:
            rows.append(f"        · (+{len(detail) - 3} more detail lines)")
    return "\n".join(rows)


def _summarize_data(data) -> str:
    if data is None:
        return "(None)"
    if isinstance(data, dict):
        keys = list(data.keys())[:10]
        return f"dict with {len(data)} key(s); first: {keys}"
    if isinstance(data, list):
        if not data:
            return "list[0] — empty"
        head = data[:3]
        return f"list[{len(data)}], first {len(head)} row(s):\n" + json.dumps(head, indent=2, default=str)
    return f"{type(data).__name__}: {str(data)[:200]}"


def run_one(prompt: str, user_role: str = "MA") -> dict:
    print(f"\n{'#' * 78}")
    print(f"# PROMPT: {prompt!r}")
    print(f"# user_role: {user_role}")
    print("#" * 78)

    from harness_entrypoint import run_query_v10

    t0 = time.monotonic()
    try:
        result = run_query_v10(prompt, user_role=user_role)
    except Exception as exc:
        elapsed = time.monotonic() - t0
        print(f"\n[!!!] Pipeline raised after {elapsed:.1f}s: {type(exc).__name__}: {exc}")
        import traceback
        traceback.print_exc()
        return {"_exception": str(exc)}

    elapsed = time.monotonic() - t0
    print(f"\n[done] pipeline returned in {elapsed:.1f}s")

    sql = result.get("validated_sql") or result.get("generated_sql") or ""
    row_count = result.get("row_count", 0)
    trace = result.get("agent_trace") or []
    formatted = result.get("formatted_response") or ""
    err = result.get("error_message") or ""
    data = result.get("query_result")

    print(_fmt_block("GENERATED / VALIDATED SQL", sql or "(none)"))
    print(_fmt_block("ROW COUNT", str(row_count)))
    print(_fmt_block("DATA (head)", _summarize_data(data)))
    print(_fmt_block("AGENT TRACE", _summarize_agent_trace(trace)))
    print(_fmt_block("FORMATTED RESPONSE", formatted or "(none)", max_lines=30))
    if err:
        print(_fmt_block("ERROR MESSAGE", err))

    return result


BATCH_PROMPTS = [
    "how many cases were created in the last 7 days",
    "list arbitrators with most active cases",
    "top 5 organizations by case count",
    "cases assigned to me with status pending",
]


# Derived-path prompts covering a varied shape spectrum (aggregates,
# JOINs, window queries, identity filters, cross-tab patterns).
DERIVED_PATH_PROMPTS = [
    "show me 5 most recent cases",
    "how many cases were created in the last 7 days",
    "list arbitrators with most active cases",
    "top 5 organizations by case count",
    "cases assigned to me with status pending",
    "what is the average resolution time per dispute type",
    "show me cases where payment is overdue by more than 30 days",
    "list the top 10 health plans by dispute volume this quarter",
    "count of closed cases this month grouped by closure reason",
    "find disputes with amount greater than $5000 that are still open",
    "show payment history for case DISP-JB4XBW8",
    "list providers with most disputed claims in the last 60 days",
    "what's the total settlement amount paid out last month",
    "show me cases waiting for arbitrator decision longer than 14 days",
    "compare ip and nip win rates by region",
]


def _verdict(result: dict) -> str:
    """One-line verdict per result for the summary table."""
    exc = (result.get("_exception") or "")[:80]
    if exc:
        return f"EXCEPTION: {exc}"
    err = (result.get("error_message") or "")[:80]
    if err:
        return f"ERROR: {err}"
    rows = result.get("row_count", 0)
    sql_len = len(result.get("validated_sql") or result.get("generated_sql") or "")
    return f"DERIVED ok · {rows} row(s) · SQL {sql_len} chars"


def run_batch(prompts, label: str = "BATCH", user_role: str = "MA"):
    """Generic batch runner over a list of prompts."""
    results = []
    for i, p in enumerate(prompts, 1):
        header = f"@@ {label} {i}/{len(prompts)}"
        print(f"\n\n{'@' * 78}\n{header}\n{'@' * 78}")
        result = run_one(p, user_role=user_role)
        result["_prompt"] = p
        results.append(result)

    print(f"\n\n{'=' * 78}\n  {label} SUMMARY ({len(prompts)} prompt(s))\n{'=' * 78}")
    for i, r in enumerate(results, 1):
        verdict = _verdict(r)
        print(f"  {i:2d}. {verdict[:90]}")
        print(f"      prompt: {r['_prompt']}")
    return results


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--derived":
        run_batch(DERIVED_PATH_PROMPTS, label="DERIVED")
        sys.exit(0)
    if len(sys.argv) > 1 and sys.argv[1] == "--batch":
        results = run_batch(BATCH_PROMPTS, label="BATCH")
        sys.exit(0 if not any(r.get("_exception") for r in results) else 2)
    prompt = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PROMPT
    user_role = sys.argv[2] if len(sys.argv) > 2 else "MA"
    result = run_one(prompt, user_role=user_role)
    sys.exit(0 if not result.get("_exception") else 2)
