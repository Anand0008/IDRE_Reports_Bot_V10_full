"""
Response Formatter Agent — V10

Display-level layout selection — narrowed to a two-shape decision
(``number`` for 1×1 scalar results, ``table`` for everything else).
Builds slide_metadata for export. Runs THREE parallel Gemini calls
via ThreadPool:
  1. query_explanation — one-line plain-English description of the SQL
  2. query_narrative — 2-4 sentence analytical narrative of the result
  3. proactive_suggestions — JSON array of 3 follow-up questions

Cell-level value formatting (currency, locale, conditional styling)
is the output_formatter's job, which runs immediately before this
node. The two are distinct concerns despite the similar names.

History:
- V6: 9-chart decision tree (number / line_chart / pie_chart /
  bar_chart / stacked_bar / scatter_plot / heatmap / funnel_chart /
  table) based on column types, cardinality, row count, and intent
  keywords; chart drill-down metadata; narrative generation;
  slide_metadata bundle for PPTX export.
- V10 (2026-05-20, user-driven): chart selection narrowed to
  ``number`` and ``table`` only. The seven chart types (heatmap,
  funnel_chart, scatter_plot, line_chart, stacked_bar, pie_chart,
  bar_chart) are disabled pending the paired output_formatter +
  response_formatter deepdive. The Altair chart branch in app.py
  (lines 616-653) reads ``chart_config`` — which is now always None —
  so it simply never renders. ``e2e_test.py`` still imports
  ``_select_chart``; the simplified version preserves the
  ``(format, chart_config_or_None)`` return shape.
"""
import json
from concurrent.futures import ThreadPoolExecutor
from tabulate import tabulate
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import HumanMessage
from config.settings import get_settings
from state.context import GraphState
from tracing import trace_agent
import re

MAX_TABLE_ROWS = 50


def _select_chart(rows: list[dict], resolved_intent: str) -> tuple:
    """Return ``(format, chart_config_or_None)`` for the result set.

    Two outcomes only:
      - ``("number", None)`` for a 1-row × 1-column scalar.
      - ``("table", None)`` for everything else, including the empty case.

    The previous V6 9-chart decision tree (heatmap / funnel / scatter /
    line / stacked_bar / pie / bar) was disabled per user direction on
    2026-05-20. ``resolved_intent`` is retained in the signature only
    for callers (e2e_test.py) and future re-enable; it is unused today.
    """
    if not rows:
        return "table", None
    if len(rows) == 1 and len(rows[0]) == 1:
        return "number", None
    return "table", None


_EXPLAINER_PROMPT = """You are a data analyst explaining a SQL query to a non-technical business user.

Given the SQL query below, write ONE clear sentence (max 30 words) explaining what it does in plain English.
Focus on WHAT data is being retrieved and any key filters — not HOW the SQL works.

Do not use technical terms like JOIN, GROUP BY, subquery, or alias.
Do not start with "This query" — start with the action (e.g., "Counts...", "Shows...", "Lists...").

SQL:
{sql}

Reply with only the one-sentence explanation, nothing else."""


_NARRATIVE_PROMPT = """You are a business analytics assistant for an IDRE dispute resolution platform.

The user asked: "{intent}"
The result had {row_count} row(s) with columns: {columns}.
Here is a sample of the data (first 5 rows): {sample_data}

Write a concise analytical narrative (2-4 sentences) that:
1. Summarizes the key finding or trend in the data
2. Highlights any notable outliers, changes, or patterns
3. Provides actionable context (e.g., "This represents a 23% increase over last month")

Be specific and use numbers from the data. Write in business language, not technical language.
Do not mention SQL, queries, or databases. Write as if presenting findings to an executive.

Reply with only the narrative paragraph, nothing else."""


_INSIGHTS_PROMPT = """You are a business analytics assistant for an IDRE dispute resolution platform.

The user asked: "{intent}"
The result had {row_count} row(s) with columns: {columns}.

Suggest exactly 3 specific follow-up questions a business analyst might ask next.
Make them concrete and directly related to the result — not generic.
Each question must be answerable from the same database.

Return ONLY a JSON array of 3 strings, no explanation, no markdown fences.
Example: ["Question 1?", "Question 2?", "Question 3?"]"""


def _extract_token_usage(response) -> dict:
    usage = getattr(response, "usage_metadata", None) or {}
    return {
        "input":  int(usage.get("input_tokens", 0)),
        "output": int(usage.get("output_tokens", 0)),
        "total":  int(usage.get("total_tokens", 0)),
    }


def _generate_explanation(sql: str) -> tuple[str, dict]:
    try:
        settings = get_settings()
        llm = ChatGoogleGenerativeAI(
            model="gemini-3.1-pro-preview",
            temperature=0.0,
            google_api_key=settings.gemini_api_key,
        )
        prompt = _EXPLAINER_PROMPT.format(sql=sql[:800])
        response = llm.invoke([HumanMessage(content=prompt)])
        content = response.content
        if isinstance(content, list):
            content = "".join(c.get("text", str(c)) if isinstance(c, dict) else str(c) for c in content)
        return content.strip().strip('"').strip("'"), _extract_token_usage(response)
    except Exception:
        return "", {}


def _generate_narrative(intent: str, rows: list[dict]) -> tuple[str, dict]:
    if not rows or len(rows) < 2:
        return "", {}
    try:
        settings = get_settings()
        llm = ChatGoogleGenerativeAI(
            model="gemini-3.1-pro-preview",
            temperature=0.2,
            google_api_key=settings.gemini_api_key,
        )
        columns = ", ".join(list(rows[0].keys())[:8])
        sample = json.dumps(rows[:5], default=str)[:600]
        prompt = _NARRATIVE_PROMPT.format(
            intent=intent[:200],
            row_count=len(rows),
            columns=columns,
            sample_data=sample,
        )
        response = llm.invoke([HumanMessage(content=prompt)])
        content = response.content
        if isinstance(content, list):
            content = "".join(c.get("text", str(c)) if isinstance(c, dict) else str(c) for c in content)
        return content.strip(), _extract_token_usage(response)
    except Exception:
        return "", {}


def _generate_suggestions(intent: str, rows: list[dict]) -> tuple[list[str], dict]:
    if not rows:
        return [], {}
    try:
        settings = get_settings()
        llm = ChatGoogleGenerativeAI(
            model="gemini-3.1-pro-preview",
            temperature=0.3,
            google_api_key=settings.gemini_api_key,
        )
        columns = ", ".join(list(rows[0].keys())[:6])
        prompt = _INSIGHTS_PROMPT.format(
            intent=intent[:200],
            row_count=len(rows),
            columns=columns,
        )
        response = llm.invoke([HumanMessage(content=prompt)])
        content = response.content
        if isinstance(content, list):
            content = "".join(c.get("text", str(c)) if isinstance(c, dict) else str(c) for c in content)
        content = content.strip()
        content = re.sub(r"^```(?:json)?\s*", "", content, flags=re.IGNORECASE)
        content = re.sub(r"\s*```$", "", content)
        suggestions = json.loads(content)
        if isinstance(suggestions, list):
            return [str(s) for s in suggestions[:3]], _extract_token_usage(response)
    except Exception:
        pass
    return [], {}


def _build_slide_metadata(
    chart_config: dict,
    explanation: str,
    narrative: str,
    rows: list[dict],
    intent: str,
) -> dict:
    if not chart_config:
        return {}
    return {
        "title": chart_config.get("title", intent[:60]),
        "subtitle": explanation or "",
        "chart_type": chart_config.get("type", "table"),
        "chart_config": chart_config,
        "narrative": narrative or "",
        "row_count": len(rows) if rows else 0,
        "data_snapshot": rows[:10] if rows else [],
    }


def _format_number(value) -> str:
    try:
        n = int(value)
        return f"{n:,}"
    except (TypeError, ValueError):
        return str(value)


def _format_assumptions(assumptions: list[str]) -> str:
    lines = "\n".join(f"> - {a}" for a in assumptions)
    return f"> **Assumptions made**\n{lines}"


def format_response(rows: list[dict], sql: str, assumptions: list[str]) -> str:
    parts = []
    if not rows:
        parts.append("No results found for your query.")
    elif len(rows) == 1 and len(rows[0]) == 1:
        col, val = next(iter(rows[0].items()))
        parts.append(f"**{col}:** {_format_number(val)}")
    elif len(rows) <= MAX_TABLE_ROWS:
        parts.append(tabulate(rows, headers="keys", tablefmt="github", floatfmt=".2f"))
        parts.append(f"\n*{len(rows)} row(s) returned.*")
    else:
        parts.append(tabulate(rows[:MAX_TABLE_ROWS], headers="keys", tablefmt="github", floatfmt=".2f"))
        parts.append(f"\n*Showing first {MAX_TABLE_ROWS} of {len(rows)} rows.*")
    if assumptions:
        parts.append("\n" + _format_assumptions(assumptions))
    # Known-path callers pass sql="" — emit no SQL block in that case so the
    # rendered response doesn't include an empty fenced code block.
    if sql:
        parts.append(f"\n**SQL used:**\n```sql\n{sql}\n```")
    return "\n".join(parts)


@trace_agent("v10.agent.response_formatter")
def response_formatter_node(state: GraphState) -> GraphState:
    rows        = state.get("query_result")
    sql         = state.get("validated_sql", state.get("generated_sql", ""))
    assumptions = state.get("assumptions", [])
    intent      = state.get("resolved_query") or state.get("user_query", "")

    fmt, chart_config = _select_chart(rows or [], intent)
    if rows and len(rows) == 1 and len(rows[0]) == 1:
        fmt = "number"
        chart_config = None

    response = format_response(rows, sql, assumptions)

    with ThreadPoolExecutor(max_workers=3) as pool:
        fut_explain = pool.submit(_generate_explanation, sql) if sql else None
        fut_suggest = pool.submit(_generate_suggestions, intent, rows or []) if rows else None
        fut_narrative = pool.submit(_generate_narrative, intent, rows or []) if rows and len(rows) >= 2 else None

        explanation, tok_explain = fut_explain.result() if fut_explain else ("", {})
        suggestions, tok_suggest = fut_suggest.result() if fut_suggest else ([], {})
        narrative, tok_narrative = fut_narrative.result() if fut_narrative else ("", {})

    token_usage = dict(state.get("token_usage") or {})
    fmt_tokens: dict[str, int] = {"input": 0, "output": 0, "total": 0}
    for tok in (tok_explain, tok_suggest, tok_narrative):
        for k in fmt_tokens:
            fmt_tokens[k] += tok.get(k, 0)
    if fmt_tokens["total"] > 0:
        token_usage["Response Formatter"] = fmt_tokens

    slide_meta = _build_slide_metadata(chart_config, explanation, narrative, rows or [], intent)

    assumption_note = f" · {len(assumptions)} assumption(s) surfaced" if assumptions else ""
    insight_note    = f" · {len(suggestions)} suggestion(s)" if suggestions else ""
    narrative_note  = " · narrative generated" if narrative else ""

    detail = []
    if chart_config:
        detail.append(f"Chart: {chart_config.get('title', fmt)}")
        if chart_config.get("drill_down"):
            detail.append(f"Drill-down: {chart_config['drill_down']['description']}")
    if explanation:
        detail.append(f"Explanation: {explanation}")
    if narrative:
        detail.append(f"Narrative: {narrative[:100]}...")
    if assumptions:
        detail.extend(assumptions)
    if slide_meta:
        detail.append("Slide export metadata generated")

    trace_entry = {
        "agent": "Response Formatter",
        "status": "ok",
        "summary": (
            f"Formatted as {fmt}"
            + (f" · {len(rows):,} rows" if rows else "")
            + assumption_note
            + insight_note
            + narrative_note
        ),
        "detail": detail,
    }
    trace = state.get("agent_trace", []) + [trace_entry]

    return {
        **state,
        "formatted_response":    response,
        "response_format":       fmt,
        "chart_config":          chart_config,
        "query_explanation":     explanation,
        "query_narrative":       narrative,
        "proactive_suggestions": suggestions,
        "slide_metadata":        slide_meta,
        "agent_trace":           trace,
        "token_usage":           token_usage,
    }
