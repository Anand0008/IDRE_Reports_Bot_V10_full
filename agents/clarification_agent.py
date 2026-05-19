"""
Clarification Agent — V10

When ambiguity_score exceeds the threshold and flags are raised, pauses
the pipeline to ask one or two short clarifying questions via Gemini.
Includes a "data preview" block (live SELECT COUNT(*) on `case` /
payment) when relevant flags fire — grounds the user in concrete
numbers like "There are 67,794 total cases, of which 24,110 are active."

On Gemini failure, halts the pipeline with the same friendly user-facing
message as context_loader's halt path (error_message + formatted_response
+ _route_after_clarification → audit_trail → END).

Threshold resolution is delegated to utils.ambiguity_threshold —
single source of truth shared with the scorer.

History:
- V6: multi-turn (max depth 2), data preview from live DB, auto-answer
  from data/clarification_history.json (keyed by lowercased query +
  flag set). NOTE: the auto-answer feature was structurally broken in
  V6/V7/V8/V9 — _save_clarification_answer was always called with
  empty answer, so _find_auto_answer never produced a cache hit.
  V10 spec §8.7 #4 explicitly flagged "Clarification auto-answered
  with heuristic dict" as a prior testing mistake to remove.
- V10: removed broken auto-answer feature (data/clarification_history.json
  no longer written or read); LLM-failure halt path identical to
  context_loader's; temperature lowered from 0.3 to 0.0 for deterministic
  clarifications; data_preview's terminal-status SQL filter sourced
  from config/business_glossary.json (one source of truth for
  "terminal status"); shared threshold helper via
  utils.ambiguity_threshold.resolve_threshold.
"""
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import SystemMessage, HumanMessage
from config.settings import get_settings
from state.context import GraphState
from agents.ambiguity_scorer import _FLAG_BY_KEY
from utils.ambiguity_threshold import resolve_threshold
from utils.glossary_matcher import find_matches as _gm_find_matches
from tracing import trace_agent


SYSTEM_PROMPT = """You are a helpful assistant for a dispute-resolution data platform.
A user asked a question that contains ambiguous or under-specified details.
Your job is to ask ONE or TWO short, plain-English clarifying questions to resolve the ambiguity.

Rules:
- Be specific: reference the exact ambiguous part of the query.
- Be concise: the entire response must be 1–3 sentences maximum.
- Give concrete options where possible (e.g. "last 7 days, 30 days, or this month?").
- Do NOT explain what you are doing — just ask the question(s).
- Do NOT use technical terms like "SQL", "filter", "schema", or "NULL".

{data_preview}

Ambiguous query: {query}

Ambiguity flags raised:
{flag_details}"""


_LLM_FAILURE_USER_MESSAGE = (
    "Sorry, something went wrong while processing your question. "
    "Please try again. If the issue persists, contact the IDRE team."
)


def _build_flag_details(flags: list[str]) -> str:
    lines = []
    for key in flags:
        flag = _FLAG_BY_KEY.get(key)
        if flag:
            lines.append(f"- {flag.label}: {flag.description}")
    return "\n".join(lines) if lines else "- General ambiguity"


def _extract_token_usage(response) -> dict:
    usage = getattr(response, "usage_metadata", None) or {}
    return {
        "input":  int(usage.get("input_tokens", 0)),
        "output": int(usage.get("output_tokens", 0)),
        "total":  int(usage.get("total_tokens", 0)),
    }


def _terminal_status_filter() -> str:
    """Lookup the canonical 'terminal status' SQL filter from glossary.

    Single source of truth — same fragment used by schema_mapper +
    sql_writer downstream. Falls back to "1=1" if the glossary entry
    is missing (degenerate — count returns all cases, which is what
    the data_preview shows anyway).
    """
    matches = _gm_find_matches("terminal status")
    for m in matches:
        if m.get("term") == "terminal status" and m.get("sql_filter"):
            return m["sql_filter"]
    return "1=1"


def _get_data_preview(query: str, flags: list[str]) -> str:
    """Best-effort live DB counts to ground the LLM's clarification."""
    previews = []
    try:
        from db.connector import get_engine
        from sqlalchemy import text
        engine = get_engine()

        if "ambiguous_closure_type" in flags or "broad_entity" in flags:
            terminal_filter = _terminal_status_filter()
            with engine.connect() as conn:
                r = conn.execute(text(
                    f"SELECT COUNT(*) FROM `case` WHERE NOT ({terminal_filter})"
                ))
                active_count = r.scalar()
                r2 = conn.execute(text("SELECT COUNT(*) FROM `case`"))
                total_count = r2.scalar()
                previews.append(
                    f"Data context: There are {total_count:,} total cases, "
                    f"of which {active_count:,} are currently active (non-terminal status)."
                )

        if "ambiguous_payment_type" in flags:
            with engine.connect() as conn:
                r = conn.execute(text(
                    "SELECT type, COUNT(*) as cnt FROM payment GROUP BY type ORDER BY cnt DESC LIMIT 5"
                ))
                rows = r.fetchall()
                if rows:
                    type_info = ", ".join(f"{row[0]}: {row[1]:,}" for row in rows)
                    previews.append(f"Payment types in system: {type_info}")
    except Exception:
        # DB unreachable / slow — data_preview is best-effort, never
        # blocking. Clarification still proceeds without numbers.
        pass

    if previews:
        return "Data context for your clarification:\n" + "\n".join(previews)
    return ""


def _generate_clarification(
    query: str, flags: list[str]
) -> tuple[str, dict] | None:
    """Generate a clarification question via Gemini.

    Returns ``(question, token_usage)`` on success, ``None`` on any
    failure. Caller halts the pipeline with the friendly user-facing
    message on None — same pattern as context_loader._resolve_query.
    """
    try:
        settings = get_settings()
        llm = ChatGoogleGenerativeAI(
            model="gemini-3.1-pro-preview",
            temperature=0.0,  # deterministic for reproducible tests
            google_api_key=settings.gemini_api_key,
        )
        data_preview = _get_data_preview(query, flags)
        system = SYSTEM_PROMPT.format(
            query=query,
            flag_details=_build_flag_details(flags),
            data_preview=data_preview,
        )
        response = llm.invoke(
            [SystemMessage(content=system),
             HumanMessage(content="Ask your clarifying question(s).")]
        )
        content = response.content
        if isinstance(content, list):
            content = "".join(
                c.get("text", str(c)) if isinstance(c, dict) else str(c)
                for c in content
            )
        text_out = content.strip()
        if not text_out:
            return None
        return text_out, _extract_token_usage(response)
    except Exception:
        return None


@trace_agent("v10.agent.clarification_agent")
def clarification_agent_node(state: GraphState) -> GraphState:
    score = state.get("ambiguity_score", 0.0)
    flags = state.get("ambiguity_flags", [])
    query = state.get("resolved_query") or state["user_query"]
    retried = state.get("clarification_attempted", False)

    user_prefs = state.get("user_preferences") or {}
    threshold = resolve_threshold(user_prefs)

    # Fast path: nothing ambiguous, or this is a re-run after the user
    # already answered a prior clarification. Just proceed.
    if retried or score <= threshold or not flags:
        reason = (
            "Re-run after clarification — skipping check"
            if retried
            else f"Score {int(score * 100)}% ≤ threshold ({int(threshold * 100)}%) — proceeding"
        )
        trace_entry = {
            "agent": "Clarification Agent",
            "status": "ok",
            "summary": reason,
            "detail": [],
        }
        trace = state.get("agent_trace", []) + [trace_entry]
        return {
            **state,
            "needs_clarification": False,
            "clarification_question": "",
            "agent_trace": trace,
        }

    # Slow path: needs a clarifying question. Call Gemini.
    result = _generate_clarification(query, flags)
    if result is None:
        # LLM failed — halt the pipeline with friendly message.
        trace_entry = {
            "agent": "Clarification Agent",
            "status": "error",
            "summary": "Clarification LLM call failed — pipeline halted",
            "detail": ["See OTel span for the underlying exception"],
        }
        trace = state.get("agent_trace", []) + [trace_entry]
        return {
            **state,
            "needs_clarification": False,
            "clarification_question": "",
            "error_message": "clarification_llm_failure",
            "formatted_response": _LLM_FAILURE_USER_MESSAGE,
            "agent_trace": trace,
        }

    question, tok = result
    token_usage = dict(state.get("token_usage") or {})
    token_usage["Clarification Agent"] = tok

    trace_entry = {
        "agent": "Clarification Agent",
        "status": "warn",
        "summary": f"Score {int(score * 100)}% — pausing pipeline to ask for clarification",
        "detail": [f"Flags: {', '.join(flags)}", f"Question: {question}"],
    }
    trace = state.get("agent_trace", []) + [trace_entry]
    return {
        **state,
        "needs_clarification": True,
        "clarification_question": question,
        "agent_trace": trace,
        "token_usage": token_usage,
    }
