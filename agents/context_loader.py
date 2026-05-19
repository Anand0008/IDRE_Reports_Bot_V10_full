"""
Context Loader Agent — V10

First node in the LangGraph derived path. Resolves the user's role to
permitted_tables, extracts entities (time_range, status_filter, org_name,
person_name, dispute_type, payment_type) from the query, and resolves
anaphora ("those cases", "what about them?") via the entity registry
first, then Gemini if needed. On LLM resolver failure, halts the
pipeline with a user-facing message via error_message +
formatted_response — _route_after_context_loader picks up the halt
signal and short-circuits to audit_trail → END.

History:
- V6: entity registry, pronoun resolution, conversation history.
- V8: removed SentenceTransformer embedding model; history relevance
  uses keyword overlap instead of cosine similarity.
- V10: grammatical registry substitutions ("those cases" → "cases with
  status PENDING_RFI", not raw enum); LLM-failure halt path with
  user-facing friendly message; tightened _REFERENCE_PATTERN to skip
  standalone "this/that/same"; status_filter regex auto-sourced from
  knowledge/v10/enum_catalog.json; dropped misleading "ES" default role
  (let permissions module own the fallback).
"""
import json
import re
from pathlib import Path
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import SystemMessage, HumanMessage
from config.settings import get_settings
from state.context import GraphState
from utils.glossary_matcher import find_matches
from utils.permissions import get_permitted_tables, get_role_display
from tracing import trace_agent

# References that imply prior-turn dependence. Standalone "this" / "that" /
# "same" / "such" are deliberately excluded — they appear in many self-contained
# queries ("this month", "that arbitrator", "the same week") and would trigger
# unnecessary LLM resolution. Multi-word forms ("those cases", "same filter")
# remain because they're unambiguous anaphora.
_REFERENCE_PATTERN = re.compile(
    r"\b("
    r"it|its|they|them|their|those|these|"
    r"the previous|the last|the above|"
    r"those cases|that query|that org|that organization|that payment type|"
    r"same filter|same status|same time|same period|same date range|same type|"
    r"what about|how about"
    r")\b"
    r"|^\s*and\b",
    re.IGNORECASE,
)


def _build_status_filter_pattern() -> "re.Pattern[str]":
    """Compile the status_filter entity regex from the live enum catalog.

    Source of truth: `knowledge/v10/enum_catalog.json` (`rds_sampled.case.status`,
    falling back to `typescript_enums.CaseStatus`). Eliminates drift between
    hand-maintained regex and IDRE's actual CaseStatus enum.
    """
    aliases = ["open", "closed", "pending", "ineligible", "eligible", "active", "terminal"]
    catalog_path = Path(__file__).parent.parent / "knowledge" / "v10" / "enum_catalog.json"
    enum_values: list[str] = []
    try:
        with open(catalog_path, encoding="utf-8") as f:
            cat = json.load(f)
        enum_values = cat.get("rds_sampled", {}).get("case.status", [])
        if not enum_values:
            enum_values = cat.get("typescript_enums", {}).get("CaseStatus", [])
    except (OSError, json.JSONDecodeError):
        pass  # Aliases-only fallback if catalog unavailable
    terms = aliases + enum_values
    return re.compile(
        r"\b(" + "|".join(re.escape(t) for t in terms) + r")\b",
        re.IGNORECASE,
    )

_ENTITY_PATTERNS = {
    "time_range": re.compile(
        r"\b(today|yesterday|this month|last month|this week|last week|"
        r"this year|last year|mtd|ytd|q[1-4]|last \d+ days|past \d+ days|"
        r"since \w+|20\d{2}|january|february|march|april|may|june|july|"
        r"august|september|october|november|december)\b", re.IGNORECASE),
    "status_filter": _build_status_filter_pattern(),
    "org_name": re.compile(
        r"\b(UHC|UnitedHealth(?:care)?|HaloMD|Halo MD|PacificHealth|"
        r"Capitol Bridge|VeraTru|Aetna|Cigna|Anthem|Humana|BCBS|"
        r"Blue Cross|Kaiser|Molina|Centene|Radix)\b", re.IGNORECASE),
    "person_name": re.compile(
        r"\b(?:assigned to|closed by|arbitrator|specialist)\s+(\w+ \w+)\b", re.IGNORECASE),
    "dispute_type": re.compile(r"\b(SINGLE|BUNDLED|BATCHED)\b", re.IGNORECASE),
    "payment_type": re.compile(
        r"\b(CASE_PAYMENT|REFUND|CAPITOL_BRIDGE_FEE|CMS_INVOICE|"
        r"THIRD_PARTY_PAYMENT|PARTY_REFUND|incoming|outgoing)\b", re.IGNORECASE),
}

def _get_relevant_history(query: str, history: list[dict], top_k: int = 3) -> list[dict]:
    """Keyword-overlap history ranking (no embedding model needed)."""
    if not history or len(history) <= top_k:
        return history

    query_words = set(query.lower().split())
    scores = []
    for i, h in enumerate(history):
        turn_text = f"{h.get('query', '')} {h.get('summary', '')}".lower()
        turn_words = set(turn_text.split())
        overlap = len(query_words & turn_words)
        scores.append((overlap, i))

    scores.sort(reverse=True)
    selected_indices = sorted([idx for _, idx in scores[:top_k]])
    return [history[i] for i in selected_indices]


def _extract_entities(query: str) -> dict[str, str]:
    entities = {}
    for entity_type, pattern in _ENTITY_PATTERNS.items():
        match = pattern.search(query)
        if match:
            entities[entity_type] = match.group(0).strip()
    return entities


def _update_entity_registry(existing: dict, new_entities: dict) -> dict:
    registry = dict(existing or {})
    registry.update(new_entities)
    return registry


def _resolve_from_registry(query: str, registry: dict) -> tuple[str, bool]:
    """Resolve anaphoric references using the entity registry.

    Substitutions produce grammatical resolved queries by wrapping the
    registry value in a context-appropriate phrase. Example:
      "show me payment totals for those cases" with
       registry["status_filter"]="PENDING_RFI"
      → "show me payment totals for cases with status PENDING_RFI"

    Raw substitution would have produced "...for PENDING_RFI" which
    downstream agents interpret as a non-existent entity.
    """
    if not registry:
        return query, False

    # (pattern, formatter, registry_key)
    replacements: list[tuple[str, "callable", str]] = [
        (r"\bthose cases\b",
         lambda v: f"cases with status {v}", "status_filter"),
        (r"\bthat org(?:anization)?\b",
         lambda v: f"organization {v}", "org_name"),
        (r"\bsame (?:time|period|date range)\b",
         lambda v: v, "time_range"),
        (r"\bsame status\b",
         lambda v: f"status {v}", "status_filter"),
        (r"\bsame type\b",
         lambda v: f"dispute type {v}", "dispute_type"),
        (r"\bthat payment type\b",
         lambda v: f"payment type {v}", "payment_type"),
    ]

    resolved = query
    changed = False
    for pattern, formatter, registry_key in replacements:
        value = registry.get(registry_key, "")
        if value and re.search(pattern, resolved, re.IGNORECASE):
            resolved = re.sub(pattern, formatter(value), resolved, flags=re.IGNORECASE)
            changed = True

    return resolved, changed


SYSTEM_PROMPT = """You are a query resolver for a data analytics chatbot about dispute resolution cases.

Given a short conversation history and a new user message, rewrite the message as a \
fully self-contained question that can be understood with no prior context.

Rules:
- Resolve pronouns: "it", "those", "them", "that" → the specific entity from history.
- Resolve references: "same filter", "same status", "those cases" → repeat the exact condition.
- Resolve follow-ups: "what about X?" or "and Y?" → expand to the full question.
- If the message is already fully self-contained (no dependency on history), return it UNCHANGED.
- Return ONLY the rewritten question — no explanation, no prefix, no punctuation changes.

Entity context from session:
{entity_context}

Conversation history (most relevant turns):
{history}"""


def _format_history(history: list[dict]) -> str:
    if not history:
        return "(none)"
    lines = []
    for i, turn in enumerate(history, 1):
        lines.append(f"Turn {i}: User asked: {turn['query']}")
        if turn.get("summary"):
            lines.append(f"         Result: {turn['summary']}")
    return "\n".join(lines)


def _format_entity_context(registry: dict) -> str:
    if not registry:
        return "(none)"
    return ", ".join(f"{k}: {v}" for k, v in registry.items())


def _needs_resolution(query: str, history: list[dict]) -> bool:
    if not history:
        return False
    return bool(_REFERENCE_PATTERN.search(query))


def _extract_token_usage(response) -> dict:
    usage = getattr(response, "usage_metadata", None) or {}
    return {
        "input":  int(usage.get("input_tokens", 0)),
        "output": int(usage.get("output_tokens", 0)),
        "total":  int(usage.get("total_tokens", 0)),
    }


def _resolve_query(
    query: str, history: list[dict], registry: dict
) -> tuple[str, dict] | None:
    """Resolve anaphora via Gemini.

    Returns ``(resolved_query, token_usage)`` on success.
    Returns ``None`` on any failure (network, rate limit, auth, malformed
    response). The caller decides how to surface the failure — currently
    the pipeline halts with a user-facing error and the agent_trace
    records a "Resolver LLM call failed" entry. The OTel span captures
    the underlying exception for operators.
    """
    settings = get_settings()
    try:
        llm = ChatGoogleGenerativeAI(
            model="gemini-3.1-pro-preview",
            temperature=0,
            google_api_key=settings.gemini_api_key,
        )
        system = SYSTEM_PROMPT.format(
            history=_format_history(history),
            entity_context=_format_entity_context(registry),
        )
        response = llm.invoke(
            [SystemMessage(content=system), HumanMessage(content=query)]
        )
        content = response.content
        if isinstance(content, list):
            content = "".join(
                c.get("text", str(c)) if isinstance(c, dict) else str(c)
                for c in content
            )
        resolved = content.strip()
        # Sanity guard: refusal text / out-of-control responses
        if not resolved or len(resolved) > 4 * len(query) + 200:
            return None
        return resolved, _extract_token_usage(response)
    except Exception:
        return None


_LLM_FAILURE_USER_MESSAGE = (
    "Sorry, something went wrong while processing your question. "
    "Please try again. If the issue persists, contact the IDRE team."
)


@trace_agent("v10.agent.context_loader")
def context_loader_node(state: GraphState) -> GraphState:
    query = state["user_query"]
    history = state.get("conversation_history", [])
    entity_registry = dict(state.get("entity_registry") or {})

    # Permissions module resolves unknown / missing role to default_role (VO).
    # Pass the raw value through — let permissions own the fallback.
    role = state.get("user_role") or ""
    permitted_tables = get_permitted_tables(role)
    role_display = get_role_display(role)

    new_entities = _extract_entities(query)
    entity_registry = _update_entity_registry(entity_registry, new_entities)

    token_usage = dict(state.get("token_usage") or {})
    resolution_method = None

    if not _needs_resolution(query, history):
        resolved = query
        changed = False
        resolution_method = "none"
    else:
        resolved, registry_resolved = _resolve_from_registry(query, entity_registry)
        if registry_resolved:
            changed = resolved.lower().strip() != query.lower().strip()
            resolution_method = "entity_registry"
        else:
            relevant_history = _get_relevant_history(query, history, top_k=3)
            llm_result = _resolve_query(query, relevant_history, entity_registry)
            if llm_result is None:
                # LLM resolver failed — halt the pipeline with a friendly
                # user-facing message. The orchestrator's
                # _route_after_context_loader sees error_message and
                # short-circuits to audit_trail → END.
                trace_entry = {
                    "agent": "Context Loader",
                    "status": "error",
                    "summary": "Resolver LLM call failed — pipeline halted",
                    "detail": ["See OTel span for the underlying exception"],
                }
                trace = state.get("agent_trace", []) + [trace_entry]
                return {
                    **state,
                    "user_role": role,
                    "permitted_tables": permitted_tables,
                    "entity_registry": entity_registry,
                    "resolved_query": query,
                    "glossary_matches": [],
                    "error_message": "context_loader_llm_failure",
                    "formatted_response": _LLM_FAILURE_USER_MESSAGE,
                    "agent_trace": trace,
                    "token_usage": token_usage,
                }
            resolved, tok = llm_result
            changed = resolved.lower().strip() != query.lower().strip()
            token_usage["Context Loader"] = tok
            resolution_method = "llm"

    resolved_entities = _extract_entities(resolved)
    entity_registry = _update_entity_registry(entity_registry, resolved_entities)

    glossary_matches = find_matches(resolved)
    glossary_terms = [m["term"] for m in glossary_matches]

    detail = []
    if changed:
        detail += [f"Original: {query}", f"Resolved: {resolved}"]
    if resolution_method and resolution_method != "none":
        detail.append(f"Resolution method: {resolution_method}")
    if new_entities:
        detail.append(f"Entities tracked: {', '.join(f'{k}={v}' for k, v in new_entities.items())}")
    if glossary_terms:
        detail.append(f"Glossary terms detected: {', '.join(glossary_terms)}")

    if not _needs_resolution(query, history) and not changed:
        summary = "No references detected — query is self-contained" if history else "First turn — no history yet"
    elif changed:
        summary = f"Query resolved via {resolution_method}"
    else:
        summary = "Query unchanged after resolution check"

    if glossary_terms:
        summary += f" · {len(glossary_terms)} glossary term(s) matched"
    summary += f" · role: {role} ({len(permitted_tables)} tables)"
    if new_entities:
        summary += f" · {len(new_entities)} entity(ies) tracked"

    detail.append(f"Role: {role_display} — {len(permitted_tables)} permitted tables")

    trace_entry = {
        "agent": "Context Loader",
        "status": "ok",
        "summary": summary,
        "detail": detail,
    }
    trace = state.get("agent_trace", []) + [trace_entry]
    return {
        **state,
        "resolved_query":   resolved,
        "glossary_matches": glossary_matches,
        "user_role":        role,
        "permitted_tables": permitted_tables,
        "entity_registry":  entity_registry,
        "agent_trace":      trace,
        "token_usage":      token_usage,
    }
