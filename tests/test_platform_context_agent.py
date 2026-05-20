"""Tests for agents/platform_context_agent.py (Option E rewrite)."""
import json
import os

KNOWLEDGE_DIR = os.path.join(os.path.dirname(__file__), "..", "knowledge", "v10")


def test_cross_cutting_rules_json_exists_with_expected_shape():
    """Universal cross-cutting rules + report-card triggers, hand-curated.

    Source: local/testing/sql-compare/DISCUSSION_SUMMARY.md §5 (8 rules)
            + 13 report cards in knowledge/data/report_reference_cards.json
    """
    path = os.path.join(KNOWLEDGE_DIR, "cross_cutting_rules.json")
    assert os.path.exists(path), "cross_cutting_rules.json must exist"
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    assert "version" in data
    assert "universal_rules" in data
    assert "report_card_triggers" in data

    # 8 cross-cutting rules from DISCUSSION_SUMMARY §5
    rules = data["universal_rules"]
    assert len(rules) >= 8, f"expected >=8 universal rules, got {len(rules)}"

    required_rule_ids = {
        "outstanding_payments", "due_date_handling", "cms_payment_type",
        "payout_entity_matching", "case_balance_formula",
        "recent_activity_window", "arbitrator_team_filter",
        "daily_funds_grouping",
    }
    rule_ids = {r["id"] for r in rules}
    missing = required_rule_ids - rule_ids
    assert not missing, f"missing rule ids: {missing}"

    for rule in rules:
        assert "id" in rule
        assert "applies_when" in rule, f"rule {rule['id']} missing applies_when"
        assert "rule" in rule, f"rule {rule['id']} missing rule body"
        assert "keywords" in rule and isinstance(rule["keywords"], list) and rule["keywords"], (
            f"rule {rule['id']} must have non-empty keywords list"
        )

    # Report-card triggers point at cards in knowledge/data/report_reference_cards.json
    triggers = data["report_card_triggers"]
    assert len(triggers) >= 10, "expected at least 10 report-card triggers"

    cards_path = os.path.join(KNOWLEDGE_DIR, "..", "data", "report_reference_cards.json")
    with open(cards_path, encoding="utf-8") as f:
        card_ids = {r["id"] for r in json.load(f)["reports"]}

    for t in triggers:
        assert "card_id" in t
        assert "keywords" in t and isinstance(t["keywords"], list) and t["keywords"]
        assert t["card_id"] in card_ids, (
            f"trigger card_id {t['card_id']} not found in report_reference_cards.json"
        )


import importlib

# Import freshly so we exercise the rewritten module.
import agents.platform_context_agent as pca


def _state(query: str, **kw):
    base = {
        "user_query": query,
        "resolved_query": query,
        "relevant_tables": [],
        "agent_trace": [],
    }
    base.update(kw)
    return base


def test_due_date_query_surfaces_4_column_or_rule():
    """A query about due dates must surface the due_date_handling rule
    AND the due-dates report card."""
    state = _state("List all overdue cases by assigned arbitrator")
    result = pca.platform_context_node(state)
    ctx = result["platform_context"]
    # universal rule should fire
    assert "due_date" in ctx and ("eligibilityDueDate" in ctx or "paymentDueDate" in ctx), (
        f"due_date_handling rule not surfaced; got: {ctx[:500]}"
    )
    # the team-performance / arbitrator rule should also fire
    assert "arbitrator" in ctx.lower(), "arbitrator_team_filter rule expected"


def test_unrelated_query_does_not_surface_capitol_bridge_rule():
    """A simple unrelated query must NOT surface the payout rule.

    Regression: avoid context bloat from over-eager keyword matching.
    """
    state = _state("How many total cases are there?")
    result = pca.platform_context_node(state)
    ctx = result["platform_context"].lower()
    assert "capitol bridge" not in ctx
    assert "bankingsnapshot" not in ctx
    assert "cms_invoice_payment" not in ctx


def test_case_balance_query_surfaces_case_refunds_rule():
    state = _state("What is the net balance per case for the top 10?")
    ctx = pca.platform_context_node(state)["platform_context"]
    assert "case_refunds" in ctx, f"case_balance_formula rule not surfaced; got: {ctx[:500]}"


def test_empty_context_when_no_keywords_match():
    """Truly unrelated query produces no rules — agent should emit empty
    platform_context (sql_writer will skip the <rules> XML block)."""
    state = _state("xyz random tokens nothing meaningful")
    result = pca.platform_context_node(state)
    assert result["platform_context"] == "" or "no specific" in result["platform_context"].lower()


def test_no_imports_from_retired_knowledge_base():
    """Regression: old knowledge_base imports must be gone."""
    import inspect
    importlib.reload(pca)
    source = inspect.getsource(pca)
    assert "from knowledge.knowledge_base" not in source
    assert "get_platform_context_for_query" not in source
    assert "search_by_concepts" not in source


def test_trace_entry_added():
    state = _state("List overdue cases by arbitrator")
    result = pca.platform_context_node(state)
    trace = result["agent_trace"]
    assert len(trace) >= 1
    last = trace[-1]
    assert last["agent"] == "Platform Context"
    assert last["status"] in ("ok", "warn")
    assert "summary" in last and "detail" in last


def test_knowledge_base_module_deleted():
    """The V7-era knowledge_base.py is removed — its sole importer was
    platform_context_agent (rewritten in Task 3); zero callers after."""
    path = os.path.join(KNOWLEDGE_DIR, "..", "knowledge_base.py")
    assert not os.path.exists(path), "knowledge/knowledge_base.py must be removed"


def test_parallel_agents_get_isolated_state_copies():
    """ARC-13 fix: schema_mapper + platform_context_node must not share
    the same state dict reference. Each sees a deep copy; merge happens
    after both complete."""
    from core.orchestrator import _parallel_schema_and_context

    # State contains a mutable list that, if shared, could race.
    seen_ids: list[int] = []

    def fake_mapper(state):
        seen_ids.append(id(state))
        return {**state, "relevant_tables": ["case"], "schema_context": "M"}

    def fake_context(state):
        seen_ids.append(id(state))
        return {**state, "platform_context": "P"}

    import core.orchestrator as orch
    orig_mapper = orch.schema_mapper_node
    orig_context = orch.platform_context_node
    orch.schema_mapper_node = fake_mapper
    orch.platform_context_node = fake_context
    try:
        state = {
            "user_query": "x", "resolved_query": "x",
            "agent_trace": [],
            "permitted_tables": [],
            "glossary_matches": [],
        }
        merged = _parallel_schema_and_context(state)
        assert merged["relevant_tables"] == ["case"]
        assert merged["schema_context"] == "M"
        assert merged["platform_context"] == "P"
        # Each agent got a distinct dict reference (deep-copy).
        assert len(seen_ids) == 2
        assert seen_ids[0] != seen_ids[1], "agents must get separate state copies"
        # Neither agent's dict id is the original state's id.
        assert id(state) not in seen_ids, "original state must not be passed by reference"
    finally:
        orch.schema_mapper_node = orig_mapper
        orch.platform_context_node = orig_context
