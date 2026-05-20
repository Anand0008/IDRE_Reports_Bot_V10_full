"""
Platform Context Agent — V10 (Node 4 rewrite, Option E)

Surfaces IDRE cross-cutting business rules + matched report-card
context for the sql_writer's <rules> block.

Two inputs:
  - `knowledge/v10/cross_cutting_rules.json` — 12 universal rules with
    explicit `keywords` arrays + 13 report-card triggers
    (lexical-match table)
  - `knowledge/data/report_reference_cards.json` — 13 report cards with
    `critical_detail`, `where_logic`, `bot_sql_equivalent`

Flow:
  1. Lowercase the query.
  2. For each universal rule, score = count of its keywords present
     as substrings in the query. Surface rules with score >= 1.
  3. For each report-card trigger, score = count of matching keywords.
     If score >= 1, load that card and surface its critical_detail +
     where_logic + bot_sql_equivalent.
  4. Cap at 4 universal rules + 2 report cards (lost-in-the-middle hygiene).
  5. Emit a formatted string into state['platform_context']; sql_writer
     wraps it in <rules>...</rules>.

History:
  - V6 introduction (Apr 26): concept extraction, staleness, confidence
    markers, section pruning. Read from knowledge/data/platform_rules.json.
  - V10 spec §3 (May 15): retired knowledge/data/ RAG inputs. The agent
    ran harmlessly with empty inputs.
  - V10 Node 4 (May 20): rewritten to read report_reference_cards.json
    (already on disk since V7) + new cross_cutting_rules.json. Old
    knowledge_base.py import dropped (the only importer was this
    agent — knowledge_base.py is removed in the same node).

Decision doc: local/docs/superpowers/reports/2026-05-20-node4-architecture-decision.md
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Any

from state.context import GraphState
from tracing import trace_agent


_RULES_PATH = os.path.join(
    os.path.dirname(__file__), "..", "knowledge", "v10", "cross_cutting_rules.json"
)
_CARDS_PATH = os.path.join(
    os.path.dirname(__file__), "..", "knowledge", "data", "report_reference_cards.json"
)

_MAX_UNIVERSAL_RULES = 4
_MAX_REPORT_CARDS = 2


@lru_cache(maxsize=1)
def _load_rules() -> dict[str, Any]:
    if not os.path.exists(_RULES_PATH):
        return {"universal_rules": [], "report_card_triggers": []}
    with open(_RULES_PATH, encoding="utf-8") as f:
        return json.load(f)


@lru_cache(maxsize=1)
def _load_cards() -> dict[str, dict[str, Any]]:
    if not os.path.exists(_CARDS_PATH):
        return {}
    with open(_CARDS_PATH, encoding="utf-8") as f:
        data = json.load(f)
    return {r["id"]: r for r in data.get("reports", [])}


def _score_keywords(query_lower: str, keywords: list[str]) -> int:
    return sum(1 for kw in keywords if kw.lower() in query_lower)


def _match_universal_rules(query_lower: str) -> list[dict[str, Any]]:
    rules = _load_rules().get("universal_rules", [])
    scored = []
    for r in rules:
        s = _score_keywords(query_lower, r.get("keywords", []))
        if s > 0:
            scored.append((s, r))
    scored.sort(key=lambda x: -x[0])
    return [r for _, r in scored[:_MAX_UNIVERSAL_RULES]]


def _match_report_cards(query_lower: str) -> list[dict[str, Any]]:
    triggers = _load_rules().get("report_card_triggers", [])
    cards_index = _load_cards()
    scored = []
    for t in triggers:
        s = _score_keywords(query_lower, t.get("keywords", []))
        if s > 0 and t["card_id"] in cards_index:
            scored.append((s, cards_index[t["card_id"]]))
    scored.sort(key=lambda x: -x[0])
    seen: set[str] = set()
    picked: list[dict[str, Any]] = []
    for _, card in scored:
        if card["id"] in seen:
            continue
        seen.add(card["id"])
        picked.append(card)
        if len(picked) >= _MAX_REPORT_CARDS:
            break
    return picked


def _format_universal(rule: dict[str, Any]) -> str:
    lines = [f"[{rule['id']}]"]
    lines.append(f"  When: {rule.get('applies_when', '')}")
    lines.append(f"  Rule: {rule.get('rule', '')}")
    snippet = rule.get("sql_snippet")
    if snippet:
        lines.append(f"  SQL snippet: {snippet}")
    return "\n".join(lines)


def _format_card(card: dict[str, Any]) -> str:
    lines = [f"[report:{card['id']}] {card.get('name', '')}"]
    tables = card.get("tables", [])
    if tables:
        lines.append(f"  Tables: {', '.join(tables)}")
    crit = card.get("critical_detail")
    if crit:
        lines.append(f"  Critical detail: {crit}")
    wlogic = card.get("where_logic")
    if wlogic:
        lines.append(f"  WHERE logic: {wlogic}")
    joins = card.get("joins")
    if joins:
        lines.append(f"  Joins: {joins}")
    sql = card.get("bot_sql_equivalent")
    if sql:
        # Truncate very long SQL to keep token budget tight.
        sql_view = sql if len(sql) <= 1200 else sql[:1200] + " /* truncated */"
        lines.append(f"  Reference SQL: {sql_view}")
    return "\n".join(lines)


@trace_agent("v10.agent.platform_context")
def platform_context_node(state: GraphState) -> GraphState:
    query = state.get("resolved_query") or state.get("user_query", "")
    query_lower = query.lower()

    universal = _match_universal_rules(query_lower)
    cards = _match_report_cards(query_lower)

    sections: list[str] = []
    if universal:
        sections.append(
            "=== IDRE CROSS-CUTTING RULES (universal) ===\n"
            + "\n\n".join(_format_universal(r) for r in universal)
        )
    if cards:
        sections.append(
            "=== IDRE REPORT-SPECIFIC RULES (matched cards) ===\n"
            + "\n\n".join(_format_card(c) for c in cards)
        )
    platform_context = "\n\n".join(sections)

    detail: list[str] = []
    if universal:
        detail.append(f"Universal rules: {', '.join(r['id'] for r in universal)}")
    if cards:
        detail.append(f"Report cards: {', '.join(c['id'] for c in cards)}")
    if not platform_context:
        summary = "No platform rules matched — using schema only"
        status = "warn"
    else:
        summary = (
            f"Surfaced {len(universal)} rule(s) + {len(cards)} report card(s)"
        )
        status = "ok"

    trace_entry = {
        "agent": "Platform Context",
        "status": status,
        "summary": summary,
        "detail": detail,
    }
    trace = state.get("agent_trace", []) + [trace_entry]

    return {
        **state,
        "platform_context": platform_context,
        "agent_trace": trace,
    }
