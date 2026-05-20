"""Tests for agents/sql_validator.py (Node 7).

Pins the safety-critical behaviour: SELECT-only enforcement, blocked
keywords, injection patterns, multi-statement, table-existence,
role-permission, column-existence. Also covers Node 7 Phase 1-3 work:
header refresh, dropped "status" semantic warning, and the dual-
catalog diagnosability hint on Unknown-table reject.

Most tests use the real root schema_catalog.json — fast, deterministic,
and the catalog is the actual source-of-truth so we don't paper over
real shape mismatches.
"""
import inspect

import agents.sql_validator as sv


# ── Phase 1 (I1 + M1): header refresh ────────────────────────────────

def test_module_header_refreshed_to_v10():
    """Node 7: stale 'SQL Validator Agent — V6' header replaced by
    V10 + a History subsection (pattern from Nodes 4/5/6)."""
    doc = sv.__doc__ or ""
    assert "V6" not in doc.split("\n")[1], "Header line must no longer say 'V6'"
    assert "SQL Validator Agent — V10" in doc, "Header must say V10"
    assert "History:" in doc, "Module docstring must include a History subsection"
    assert "Six blocking checks" in doc, "Docstring undercount 'Five checks' must be corrected"


# ── Phase 2 (M2): "status" semantic warning gone ─────────────────────

def test_status_semantic_warning_dropped():
    """Node 7: the 'user mentioned status but SQL doesn't reference it'
    rule was a high-false-positive heuristic; removed."""
    # Query talks about status; SQL deliberately omits any status reference.
    warnings = sv._semantic_validation(
        sql="SELECT id FROM `case` WHERE id = 1",
        query="show me the status of recent disputes",
    )
    assert not any("status" in w.lower() for w in warnings), (
        f"No 'status'-mention semantic warning expected after Node 7 drop, got: {warnings}"
    )
    # The other three semantic checks must still fire when appropriate.
    warnings = sv._semantic_validation(
        sql="SELECT id FROM `case`",
        query="show me the top 5 cases",
    )
    assert any("top" in w.lower() and "LIMIT" in w for w in warnings), (
        "top-N-without-LIMIT semantic check must still fire"
    )


# ── Phase 3 (I2): dual-catalog diagnosability hint ───────────────────

def test_unknown_table_reject_includes_diagnostic_hint():
    """Node 7: the Unknown-table reject now points the user at the
    Phase-5 Design-A refresh path so debugging the dual-catalog gap
    isn't a research project."""
    is_valid, error = sv.validate_sql(
        "SELECT * FROM totally_made_up_table_that_does_not_exist"
    )
    assert is_valid is False
    assert "Unknown table" in error
    # The hint:
    assert "stale" in error.lower(), "Reject message must mention catalog staleness"
    assert "analyze_db.py" in error, "Reject message must surface the refresh command"
    assert "Phase 5" in error or "Design A" in error, (
        "Reject must point at the Phase 5 Design A roadmap"
    )


# ── Core happy path ──────────────────────────────────────────────────

def test_simple_select_validates():
    is_valid, error = sv.validate_sql("SELECT id FROM `case` LIMIT 5")
    assert is_valid is True, f"Simple SELECT must validate, got error: {error}"
    assert error == ""


def test_select_with_join_validates():
    is_valid, error = sv.validate_sql(
        "SELECT c.id, p.amount FROM `case` c "
        "JOIN case_payment_allocation cpa ON cpa.caseId = c.id "
        "JOIN payment p ON p.id = cpa.paymentId"
    )
    assert is_valid is True, f"Multi-JOIN SELECT must validate, got error: {error}"


# ── Statement-shape rejects ──────────────────────────────────────────

def test_empty_sql_rejected():
    is_valid, error = sv.validate_sql("")
    assert is_valid is False
    assert "Empty" in error


def test_non_select_rejected():
    is_valid, error = sv.validate_sql("SHOW TABLES")
    assert is_valid is False
    # 2026-05-21: error message extended to "SELECT or WITH (CTE)" after
    # CTE support landed. The shared substring still pins the rejection.
    assert "must be a SELECT" in error


def test_ddl_rejected():
    is_valid, error = sv.validate_sql("SELECT id FROM `case`; DROP TABLE `case`")
    assert is_valid is False
    # Either multi-statement or blocked-keyword reject is fine — both indicate
    # the safety gate caught it. We just need the gate to refuse.
    assert any(s in error for s in ("Blocked keyword", "Multiple SQL statements", "DROP"))


def test_dml_rejected():
    is_valid, error = sv.validate_sql("UPDATE `case` SET status = 'CLOSED'")
    assert is_valid is False
    # Must be rejected either at statement-type (not SELECT) or blocked-keyword stage
    assert any(s in error for s in ("must be a SELECT", "Blocked keyword 'UPDATE'"))


def test_multi_statement_rejected():
    is_valid, error = sv.validate_sql("SELECT 1; SELECT 2")
    assert is_valid is False
    assert "Multiple SQL statements" in error or "Blocked" in error


# ── Injection rejects ────────────────────────────────────────────────

def test_or_1_equals_1_rejected():
    is_valid, error = sv.validate_sql("SELECT id FROM `case` WHERE 1=1 OR 1=1")
    assert is_valid is False
    assert "injection" in error.lower()


def test_union_select_accepted():
    """UNION / UNION ALL composition is legitimate SQL (used by the SQL
    Writer for cross-FK aggregates like organization counts via both
    initiatingPartyOrganizationId + nonInitiatingPartyOrganizationId).
    The 2026-05-21 fix removed the over-broad UNION-as-injection pattern
    because the bot has no user-controlled string concat into SQL — the
    threat model doesn't apply here. BLOCKED_KEYWORDS + RBAC + EXPLAIN
    dry-run cover the real surface.
    """
    is_valid, error = sv.validate_sql(
        "SELECT id FROM `case` UNION ALL SELECT id FROM `case_party`"
    )
    assert is_valid is True, f"UNION ALL composition should validate; got error: {error}"


def test_sleep_injection_rejected():
    is_valid, error = sv.validate_sql("SELECT SLEEP(10) FROM `case`")
    assert is_valid is False
    assert "injection" in error.lower()


# ── Role-permission reject ───────────────────────────────────────────

def test_role_permission_violation_rejected():
    """If permitted_tables is supplied and SQL references something
    outside it, reject before execution."""
    is_valid, error = sv.validate_sql(
        "SELECT id FROM `case`",
        permitted_tables=["payment", "organization"],  # `case` not permitted
    )
    assert is_valid is False
    assert "not accessible" in error.lower() or "role" in error.lower()


# ── Hallucinated-column reject (via node-level dispatch) ─────────────

def test_hallucinated_column_blocked_by_node():
    """validate_columns is informational; sql_validator_node escalates
    hallucinated columns to a blocking error_message."""
    state = {
        "generated_sql": "SELECT `case`.nonexistent_column_xyz FROM `case`",
        "permitted_tables": None,
        "user_query": "show me the made-up column",
        "resolved_query": "",
        "agent_trace": [],
    }
    out = sv.sql_validator_node(state)
    assert out["validated_sql"] == ""
    assert "Hallucinated column" in (out.get("error_message") or "")


# ── Cost-estimate flag (non-blocking) ────────────────────────────────

def test_expensive_cost_estimate_does_not_block():
    """A multi-million-row-scan estimate must flag without blocking the
    query — the live EXPLAIN gate (Node 6) is the real performance check."""
    # case_action has ~499K rows; 3 joins → > 1M estimated scan.
    state = {
        "generated_sql": (
            "SELECT a.id FROM case_action a "
            "JOIN case_action b ON b.caseId = a.caseId "
            "JOIN case_action c ON c.caseId = b.caseId"
        ),
        "permitted_tables": None,
        "user_query": "x", "resolved_query": "",
        "agent_trace": [],
    }
    out = sv.sql_validator_node(state)
    # Validates (no error_message), but the trace entry should reflect cost.
    assert out["validated_sql"] != ""
    assert out["error_message"] == ""
    last_trace = out["agent_trace"][-1]
    assert last_trace["agent"] == "SQL Validator"
    # status may be "warn" (expensive) or "ok"; we don't pin the threshold
    # behaviour itself — just that the cost gate ran without blocking.


# ── Helper: _extract_table_names with backticks + JOIN ───────────────

def test_extract_table_names_handles_backticks_and_joins():
    tables = sv._extract_table_names(
        "SELECT * FROM `case` c JOIN `payment` p ON p.id = c.id"
    )
    assert "case" in tables, f"Backticked FROM table missed: {tables}"
    assert "payment" in tables, f"Backticked JOIN table missed: {tables}"


# ── Sanity: public surface preserved ─────────────────────────────────

def test_public_surface_preserved():
    """Capability pin — refactors must preserve these names."""
    for name in (
        "validate_sql",
        "validate_columns",
        "sql_validator_node",
        "BLOCKED_KEYWORDS",
        "_INJECTION_PATTERNS",
        "_extract_table_names",
        "_estimate_cost",
        "_semantic_validation",
        "_check_injection",
        "_check_type_compatibility",
    ):
        assert hasattr(sv, name), f"Public surface lost: {name}"
