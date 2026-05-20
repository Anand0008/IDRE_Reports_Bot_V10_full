"""Tests for agents/sql_writer.py (Option E: SYSTEM_PROMPT + XML injection)."""
import re
from types import SimpleNamespace

import agents.sql_writer as sw


def test_system_prompt_includes_all_eight_cross_cutting_rules():
    """V6-style restoration: the cached SYSTEM_PROMPT must name each
    of the 8 cross-cutting rules so the LLM can spot when to apply
    them even if Layer 2's <rules> block missed surfacing one."""
    sp = sw.SYSTEM_PROMPT
    for token in [
        "Outstanding payments",
        "Due dates",
        "CMS payments",
        "Payouts",
        "Case balance",
        "Recent activity",
        "Arbitrator",
        "Daily funds",
    ]:
        assert token in sp, f"SYSTEM_PROMPT missing reference to: {token}"


def test_system_prompt_includes_display_rules():
    sp = sw.SYSTEM_PROMPT
    assert "shortId" in sp, "shortId display rule must be in SYSTEM_PROMPT"
    assert "dispute_number" in sp, "dispute_number alias rule must be named"
    assert "dispute_line_items" in sp and "ACTIVE" in sp, (
        "soft-delete rule for dispute_line_items must be in SYSTEM_PROMPT"
    )


def test_system_prompt_includes_join_skeletons():
    sp = sw.SYSTEM_PROMPT
    assert "case_payment_allocation" in sp, "payment JOIN skeleton expected"
    assert "nonInitiatingPartyOrganizationId" in sp, "NIP JOIN skeleton expected"
    assert "initiatingPartyOrganizationId" in sp, "IP JOIN skeleton expected"
    assert "assignedToId" in sp, "user/arbitrator JOIN skeleton expected"


def test_system_prompt_keeps_mandatory_verification():
    """The MANDATORY PROTOCOL must keep verify_sql_executes."""
    assert "verify_sql_executes" in sw.SYSTEM_PROMPT


def test_max_verification_rounds_constant_drives_prompt_and_code():
    """Node 6 I2: MAX_VERIFICATION_ROUNDS is the single source of truth.
    The SYSTEM_PROMPT prose must quote the same value the tool-call loop
    enforces — no prompt-vs-code mismatch."""
    import inspect

    assert hasattr(sw, "MAX_VERIFICATION_ROUNDS"), (
        "MAX_VERIFICATION_ROUNDS module constant must be defined in sql_writer"
    )
    n = sw.MAX_VERIFICATION_ROUNDS
    assert isinstance(n, int) and n >= 1

    # Prompt text must mention the actual ceiling
    assert f"Max {n} verification rounds" in sw.SYSTEM_PROMPT, (
        f"SYSTEM_PROMPT must quote 'Max {n} verification rounds' "
        f"(MAX_VERIFICATION_ROUNDS constant), got prompt without it"
    )
    # The historical "Max 3" wording must not survive — it's the stale value
    # this fix exists to eliminate.
    assert "Max 3 verification rounds" not in sw.SYSTEM_PROMPT, (
        "Stale 'Max 3 verification rounds' must be replaced by the constant"
    )

    # The code-side default must also point at the constant, not a literal
    sig = inspect.signature(sw._generate_sql_with_tools)
    default = sig.parameters["max_tool_rounds"].default
    assert default == n, (
        f"_generate_sql_with_tools(max_tool_rounds=) default must equal "
        f"MAX_VERIFICATION_ROUNDS ({n}), got {default}"
    )


def test_system_prompt_keeps_hard_rules():
    sp = sw.SYSTEM_PROMPT
    assert re.search(r"SELECT-only", sp, re.IGNORECASE), "SELECT-only HARD RULE missing"
    assert re.search(r"backtick.*case", sp, re.IGNORECASE) or "`case`" in sp, (
        "Backtick-case HARD RULE missing"
    )


def test_system_prompt_length_within_budget():
    """Target ~70 lines; cap at 120 for token-budget hygiene."""
    line_count = len(sw.SYSTEM_PROMPT.splitlines())
    assert 50 <= line_count <= 120, f"SYSTEM_PROMPT length {line_count} outside [50, 120]"


def test_build_user_message_wraps_query_in_xml():
    state = {
        "user_query": "List overdue cases",
        "resolved_query": "List overdue cases",
        "schema_context": "",
        "platform_context": "",
        "glossary_matches": [],
        "permitted_tables": [],
        "user_role": "MA",
    }
    msg = sw._build_user_message(state, error_context="")
    assert "<query>" in msg and "</query>" in msg
    assert "List overdue cases" in msg


def test_build_user_message_includes_schema_block_when_present():
    state = {
        "user_query": "x",
        "resolved_query": "x",
        "schema_context": "Table: case\nColumns: id, status, due_date",
        "platform_context": "",
        "glossary_matches": [],
        "permitted_tables": [],
        "user_role": "MA",
    }
    msg = sw._build_user_message(state, error_context="")
    assert "<schema>" in msg and "</schema>" in msg
    assert "Table: case" in msg


def test_build_user_message_includes_rules_block_when_platform_context_present():
    state = {
        "user_query": "x", "resolved_query": "x",
        "schema_context": "",
        "platform_context": "=== IDRE CROSS-CUTTING RULES ===\n[due_date_handling]\n  Rule: ...",
        "glossary_matches": [], "permitted_tables": [], "user_role": "MA",
    }
    msg = sw._build_user_message(state, error_context="")
    assert "<rules>" in msg and "</rules>" in msg
    assert "due_date_handling" in msg


def test_build_user_message_omits_empty_blocks():
    """If schema_context / platform_context are empty, do NOT emit empty
    <schema></schema> blocks — they waste tokens and confuse the LLM."""
    state = {
        "user_query": "x", "resolved_query": "x",
        "schema_context": "", "platform_context": "",
        "glossary_matches": [], "permitted_tables": [], "user_role": "MA",
    }
    msg = sw._build_user_message(state, error_context="")
    assert "<schema>" not in msg, "empty schema_context must not emit a <schema> block"
    assert "<rules>" not in msg, "empty platform_context must not emit a <rules> block"


def test_build_user_message_includes_role_and_permitted_tables():
    state = {
        "user_query": "x", "resolved_query": "x",
        "schema_context": "", "platform_context": "",
        "glossary_matches": [], "permitted_tables": ["case", "payment"],
        "user_role": "PA",
    }
    msg = sw._build_user_message(state, error_context="")
    assert "<role>" in msg and "</role>" in msg
    assert "PA" in msg
    assert "case" in msg and "payment" in msg


def test_build_user_message_appends_error_context_when_retry():
    state = {
        "user_query": "x", "resolved_query": "x",
        "schema_context": "", "platform_context": "",
        "glossary_matches": [], "permitted_tables": [], "user_role": "MA",
    }
    msg = sw._build_user_message(state, error_context="Column 'foo' not found")
    assert "Previous attempt failed" in msg or "Column 'foo'" in msg


def test_sql_writer_node_passes_xml_message_to_gemini(monkeypatch):
    """Integration: sql_writer_node must hand the LLM a user message that
    contains <schema> + <rules> when state has them populated."""
    captured = {}

    class _FakeResp:
        candidates = []
        usage_metadata = None

    class _FakeChat:
        def send_message(self, message):
            captured["message"] = message
            return _FakeResp()

    class _FakeModel:
        def __init__(self, *a, **kw):
            captured["system_instruction"] = kw.get("system_instruction", "")

        def start_chat(self):
            return _FakeChat()

    import google.generativeai as genai
    monkeypatch.setattr(genai, "GenerativeModel", _FakeModel)
    monkeypatch.setattr(genai, "configure", lambda **kw: None)

    # Skip the EXPLAIN call by stubbing _check_explain_plan
    monkeypatch.setattr(sw, "_check_explain_plan", lambda sql: {"total_rows": 0, "full_scan_tables": [], "warning": False})

    state = {
        "user_query": "List overdue cases by arbitrator",
        "resolved_query": "List overdue cases by arbitrator",
        "schema_context": "Table: case\nColumns: id, status, due_date, assignedToId",
        "platform_context": "=== IDRE CROSS-CUTTING RULES ===\n[due_date_handling]\n  Rule: OR 4 cols",
        "glossary_matches": [],
        "permitted_tables": ["case", "user"],
        "user_role": "MA",
        "agent_trace": [],
        "retry_count": 0,
        "token_usage": {},
        "retry_context": "",
        "execution_error": None,
    }
    sw.sql_writer_node(state)
    msg = captured.get("message", "")
    assert "<query>" in msg and "List overdue cases by arbitrator" in msg
    assert "<schema>" in msg and "Table: case" in msg
    assert "<rules>" in msg and "due_date_handling" in msg
    assert "<role>" in msg and "MA" in msg


# ── Node 6.1 D1 — Tiered SQL parser (fenced → prefix → line → empty) ─


def test_parser_assumption_with_word_no_longer_misread_as_sql():
    """The S1 regression: `_is_sql` matched plain English 'with' so
    `users with roles arbitrator` was returned as SQL. The tiered parser
    rejects it — neither half STARTS with SELECT/WITH at a line."""
    raw = (
        "ASSUMPTIONS:\n"
        "- Arbitrators are users with roles arbitrator\n"
        "- Active cases not in terminal status"
    )
    sql, assumptions = sw._parse_llm_response(raw)
    assert sql == "", (
        f"Smoke S1: assumption text containing 'with' must NOT be returned as SQL, got {sql!r}"
    )
    assert len(assumptions) >= 2, "Assumption bullets should still be captured"


def test_parser_prose_mentioning_select_keyword_no_longer_misread_as_sql():
    """`I will SELECT recent cases` (prose) is not SQL — must return empty."""
    raw = (
        "ASSUMPTIONS:\n"
        "- I will SELECT the most recent cases\n"
        "- Sort descending by createdAt"
    )
    sql, _ = sw._parse_llm_response(raw)
    assert sql == "", f"Prose containing 'SELECT' must not be returned as SQL, got {sql!r}"


def test_parser_assumption_status_update_phrase_no_longer_misread_as_sql():
    """`status update` is plain English — `\\bUPDATE\\b` matched in the old code."""
    raw = (
        "ASSUMPTIONS:\n"
        "- The case status update timestamp is statusChangedAt"
    )
    sql, _ = sw._parse_llm_response(raw)
    assert sql == "", f"'status update' prose must not be returned as SQL, got {sql!r}"


def test_parser_fenced_sql_block_extracts_correctly():
    """Tier 1 (fenced) — the happy path stays working."""
    raw = (
        "ASSUMPTIONS:\n"
        "- Most recent means latest createdAt\n"
        "```sql\n"
        "SELECT c.shortId FROM `case` c ORDER BY c.createdAt DESC LIMIT 5\n"
        "```"
    )
    sql, assumptions = sw._parse_llm_response(raw)
    assert sql.startswith("SELECT c.shortId")
    assert any("Most recent" in a for a in assumptions)


def test_parser_unfenced_sql_starting_with_select_is_extracted():
    """Tier 2 (prefix-anchored) — no fence, but the after-half starts with SELECT."""
    raw = (
        "ASSUMPTIONS:\n"
        "- Filter by status pending\n"
        "\n"
        "SELECT id FROM `case` WHERE status LIKE 'PENDING%'"
    )
    sql, _ = sw._parse_llm_response(raw)
    assert sql.startswith("SELECT id FROM `case`"), f"got {sql!r}"


def test_parser_unfenced_sql_starting_with_with_cte_is_extracted():
    """Tier 2 also handles WITH (CTE) prefix."""
    raw = (
        "ASSUMPTIONS:\n"
        "- Use a CTE for the join\n"
        "\n"
        "WITH recent_cases AS (\n"
        "  SELECT id FROM `case`\n"
        ") SELECT * FROM recent_cases"
    )
    sql, _ = sw._parse_llm_response(raw)
    assert sql.startswith("WITH recent_cases"), f"got {sql!r}"


def test_parser_inline_select_after_prose_line_anchored():
    """Tier 3 (line-anchored) — Gemini emits prose + then a new line
    starting with SELECT, no fence. The parser finds the SELECT line
    and treats everything from that line as the SQL."""
    raw = (
        "Here's the query that answers your question.\n"
        "\n"
        "SELECT COUNT(*) FROM `case` WHERE createdAt >= DATE_SUB(:now, INTERVAL 7 DAY)"
    )
    sql, _ = sw._parse_llm_response(raw)
    assert sql.startswith("SELECT COUNT(*)"), f"got {sql!r}"


def test_parser_no_sql_anywhere_returns_empty():
    """If neither fenced nor any line starts with SELECT/WITH, return empty."""
    raw = (
        "I cannot answer this question with the available schema. "
        "There's no table for organizational structure that I can see."
    )
    sql, _ = sw._parse_llm_response(raw)
    assert sql == ""


# ── Node 6.1 D2 — MALFORMED_FUNCTION_CALL exception handling ─────────


def test_generate_sql_with_tools_initial_send_exception_returns_empty(monkeypatch):
    """The very first chat.send_message(user_message) call can raise.
    Verify the function returns empty SQL instead of crashing."""
    from google.generativeai.types.generation_types import StopCandidateException

    class _FakeChat:
        def send_message(self, _):
            raise StopCandidateException("finish_reason: MALFORMED_FUNCTION_CALL")

    class _FakeModel:
        def __init__(self, *a, **kw): pass
        def start_chat(self): return _FakeChat()

    import google.generativeai as genai
    monkeypatch.setattr(genai, "GenerativeModel", _FakeModel)
    monkeypatch.setattr(genai, "configure", lambda **kw: None)

    sql, assumptions, tokens, tool_log = sw._generate_sql_with_tools({
        "user_query": "anything", "resolved_query": "anything",
        "schema_context": "", "platform_context": "",
        "permitted_tables": [], "user_role": "MA",
    })
    assert sql == "", f"Exception path must return empty SQL, got {sql!r}"
    assert any("_gemini_error_marker" in tc.get("tool", "") for tc in tool_log), (
        "tool_calls_log must contain the exception marker"
    )


def test_generate_sql_with_tools_inloop_send_exception_returns_empty(monkeypatch):
    """When the FIRST send_message succeeds (returning a function-call
    response) but the SECOND send_message (sending tool results back) raises,
    we still return empty SQL — not crash."""
    from google.generativeai.types.generation_types import StopCandidateException

    class _FakeFunctionCall:
        def __init__(self, name):
            self.name = name
            self.args = {}

    class _FakePart:
        def __init__(self, fn_name):
            self.function_call = _FakeFunctionCall(fn_name)
            self.text = ""

    class _FakeContent:
        def __init__(self, parts):
            self.parts = parts

    class _FakeCandidate:
        def __init__(self, parts):
            self.content = _FakeContent(parts)

    class _FakeResp:
        def __init__(self, parts):
            self.candidates = [_FakeCandidate(parts)] if parts else []
            self.usage_metadata = None

    calls = {"count": 0}

    class _FakeChat:
        def send_message(self, _):
            calls["count"] += 1
            if calls["count"] == 1:
                # First call: Gemini decides to invoke a no-arg tool
                # (list_available_reports() has no required args)
                return _FakeResp([_FakePart("list_available_reports")])
            # Second call (with the tool response): SDK raises
            raise StopCandidateException("MALFORMED_FUNCTION_CALL")

    class _FakeModel:
        def __init__(self, *a, **kw): pass
        def start_chat(self): return _FakeChat()

    import google.generativeai as genai
    monkeypatch.setattr(genai, "GenerativeModel", _FakeModel)
    monkeypatch.setattr(genai, "configure", lambda **kw: None)

    sql, assumptions, tokens, tool_log = sw._generate_sql_with_tools({
        "user_query": "x", "resolved_query": "x",
        "schema_context": "", "platform_context": "",
        "permitted_tables": [], "user_role": "MA",
    })
    assert sql == ""
    # One real tool call (get_table_schema) PLUS the exception marker
    assert any("_gemini_error_marker" in tc.get("tool", "") for tc in tool_log)


# ── tool_calls_log propagation through state (2026-05-21) ─────────────


def test_sql_writer_node_surfaces_tool_calls_log_to_state(monkeypatch):
    """sql_writer_node must propagate the tool_calls list from
    _generate_sql_with_tools into state['tool_calls_log'] so audit +
    feedback writers can capture it."""
    def fake_generate(state, error_context="", max_tool_rounds=5):
        tool_calls = [
            {"tool": "verify_sql_executes", "args": {"sql": "..."}, "result_length": 120},
            {"tool": "get_table_schema", "args": {"table_name": "case"}, "result_length": 800},
        ]
        return "SELECT 1 AS x", ["no assumptions"], {"input": 10, "output": 20, "total": 30}, tool_calls

    monkeypatch.setattr(sw, "_generate_sql_with_tools", fake_generate)
    monkeypatch.setattr(sw, "_check_explain_plan",
                        lambda sql: {"total_rows": 0, "full_scan_tables": [], "warning": False})

    state = {
        "user_query": "show me 5 cases", "resolved_query": "show me 5 cases",
        "schema_context": "case table", "platform_context": "",
        "permitted_tables": ["case"], "user_role": "MA",
        "retry_count": 0, "agent_trace": [], "token_usage": {},
        "tool_calls_log": [],
    }
    out = sw.sql_writer_node(state)
    assert "tool_calls_log" in out
    assert isinstance(out["tool_calls_log"], list)
    assert len(out["tool_calls_log"]) == 2
    assert out["tool_calls_log"][0]["tool"] == "verify_sql_executes"


def test_sql_writer_node_appends_tool_calls_log_across_retries(monkeypatch):
    """Two sequential sql_writer_node calls — the second's tool_calls
    must be appended to the first's, not replace them."""
    call_count = {"n": 0}

    def fake_generate(state, error_context="", max_tool_rounds=5):
        call_count["n"] += 1
        if call_count["n"] == 1:
            tool_calls = [{"tool": "get_table_schema", "args": {}, "result_length": 100}]
        else:
            tool_calls = [{"tool": "verify_sql_executes", "args": {}, "result_length": 200}]
        return "SELECT 1", [], {"input": 0, "output": 0, "total": 0}, tool_calls

    monkeypatch.setattr(sw, "_generate_sql_with_tools", fake_generate)
    monkeypatch.setattr(sw, "_check_explain_plan",
                        lambda sql: {"total_rows": 0, "full_scan_tables": [], "warning": False})

    state = {
        "user_query": "q", "resolved_query": "q",
        "schema_context": "", "platform_context": "",
        "permitted_tables": [], "user_role": "MA",
        "retry_count": 0, "agent_trace": [], "token_usage": {},
        "tool_calls_log": [],
    }
    state = sw.sql_writer_node(state)
    state["retry_count"] = 1
    state = sw.sql_writer_node(state)
    assert len(state["tool_calls_log"]) == 2
    assert state["tool_calls_log"][0]["tool"] == "get_table_schema"
    assert state["tool_calls_log"][1]["tool"] == "verify_sql_executes"
