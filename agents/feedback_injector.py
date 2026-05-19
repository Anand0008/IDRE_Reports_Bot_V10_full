"""
Feedback Injector Agent — V10

Runs on the feedback-retry path (Flow B): when a user marks an answer as
wrong and triggers a re-run, this node prepends a structured "correction
context" block to ``state.retry_context`` so the SQL Writer's next attempt
knows what the user said was wrong.

V10 cleanup vs. V6: removed the "propose-metric-card" side effect — V10
retired ``metric_cards.json`` entirely (spec §3), so writing proposals
into a file nothing reads was dead code. Error-category weighting + past
feedback summaries remain.
"""
import json
import os
from state.context import GraphState
from tracing import trace_agent

_DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
_ERROR_WEIGHTS_PATH = os.path.join(_DATA_DIR, "error_category_weights.json")
_CORRECTION_LOG_PATH = os.path.join(_DATA_DIR, "correction_success_log.json")


def _load_json(path: str, default=None):
    if not os.path.exists(path):
        return default if default is not None else {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default if default is not None else {}


def _save_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)


def _load_error_weights() -> dict[str, float]:
    data = _load_json(_ERROR_WEIGHTS_PATH, {})
    return data.get("weights", {})


def _get_correction_count(query_pattern: str) -> int:
    log = _load_json(_CORRECTION_LOG_PATH, {"corrections": []})
    return sum(1 for c in log.get("corrections", []) if c.get("pattern", "").lower() == query_pattern.lower())


def _log_correction(query: str, categories: list[str]) -> None:
    log = _load_json(_CORRECTION_LOG_PATH, {"corrections": []})
    log["corrections"].append({
        "pattern": query,
        "categories": categories,
    })
    _save_json(_CORRECTION_LOG_PATH, log)


def _get_feedback_patterns_summary() -> str:
    """Summarize past feedback patterns for the SQL Writer."""
    log = _load_json(_CORRECTION_LOG_PATH, {"corrections": []})
    corrections = log.get("corrections", [])
    if not corrections:
        return ""

    category_counts: dict[str, int] = {}
    for c in corrections:
        for cat in c.get("categories", []):
            category_counts[cat] = category_counts.get(cat, 0) + 1

    if not category_counts:
        return ""

    top_cats = sorted(category_counts.items(), key=lambda x: -x[1])[:5]
    lines = ["PAST FEEDBACK PATTERNS (common errors to avoid):"]
    for cat, count in top_cats:
        lines.append(f"  - {cat}: reported {count} time(s)")
    return "\n".join(lines)


def _format_correction_block(correction: dict) -> str:
    categories = correction.get("error_categories", [])
    note = correction.get("free_text_note", "").strip()
    original = correction.get("original_query", "").strip()
    summary = correction.get("original_result_summary", "").strip()

    weights = _load_error_weights()
    if weights and categories:
        categories = sorted(categories, key=lambda c: weights.get(c, 0.5), reverse=True)

    lines = [
        "FEEDBACK CORRECTION (user-reported error):",
        f"Original question: {original}",
    ]

    if summary:
        lines.append(f"Previous answer shown: {summary[:200]}")

    if categories:
        lines.append("What the user reported was wrong (ordered by severity):")
        for c in categories:
            weight = weights.get(c, 0.5)
            lines.append(f"  - {c} (priority: {weight:.1f})")

    if note:
        lines.append(f'User note: "{note}"')

    lines += [
        "",
        "Instruction: The user has already seen one answer to this question and marked it "
        "as incorrect. Address each reported issue above directly and explicitly. "
        "Do NOT repeat the same approach as before. Treat this as a high-priority correction.",
    ]

    feedback_patterns = _get_feedback_patterns_summary()
    if feedback_patterns:
        lines += ["", feedback_patterns]

    return "\n".join(lines)


@trace_agent("v10.agent.feedback_injector")
def feedback_injector_node(state: GraphState) -> GraphState:
    correction = state.get("feedback_correction_context") or {}
    if not correction:
        return state

    correction_block = _format_correction_block(correction)

    _log_correction(
        correction.get("original_query", ""),
        correction.get("error_categories", []),
    )

    count = _get_correction_count(correction.get("original_query", ""))

    existing = state.get("retry_context", "") or ""
    new_retry_context = correction_block + ("\n\n" + existing if existing else "")

    detail = [f"Error categories: {correction.get('error_categories', [])}"]
    if count >= 3:
        detail.append(f"Repeated correction (#{count} for this pattern)")

    trace_entry = {
        "agent": "Feedback Injector",
        "status": "warn",
        "summary": f"Correction context injected · {len(correction.get('error_categories', []))} error type(s)",
        "detail": detail,
    }

    return {
        **state,
        "retry_context": new_retry_context,
        "agent_trace": state.get("agent_trace", []) + [trace_entry],
    }
