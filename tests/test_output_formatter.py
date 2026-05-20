"""Tests for agents/output_formatter.py (Node 10).

Pins the cell-level value formatting + the Node 10 cleanups:
- Phase 1: V10 header refresh
- Phase 2 (smoke S3): _TIME_WINDOW_SUFFIX_RE intercepts `_last_N_days`
  / `_in_N_days` etc. so the trailing time-window qualifier doesn't
  mis-classify a COUNT result as a duration.
- Phase 3: first-time coverage on _detect_col_type, the formatters,
  conditional formatting, _format_rows end-to-end, output_formatter_node.
"""
from datetime import date, datetime
from decimal import Decimal

import agents.output_formatter as of


# ── Phase 1: V10 header ──────────────────────────────────────────────

def test_module_header_refreshed_to_v10():
    doc = of.__doc__ or ""
    first_line = doc.strip().splitlines()[0]
    assert "V6" not in first_line, f"First header line still references V6: {first_line!r}"
    assert "Output Formatter Agent — V10" in doc
    assert "History:" in doc, "Module docstring must include a History subsection"
    # The S3 fix must be mentioned in the V10 Node 10 entry
    assert "Node 10" in doc and "time-window" in doc.lower(), (
        "History must document the Node 10 S3 fix"
    )


# ── Phase 2: smoke S3 — time-window suffix regression ────────────────

def test_smoke_s3_cases_created_last_7_days_is_count_not_days():
    """The bug that produced '1,020 days' for a COUNT result."""
    got = of._detect_col_type("cases_created_last_7_days", [1020])
    assert got == "count", (
        f"Smoke S3 regression — cases_created_last_7_days(int) must be count, got {got}"
    )


def test_time_window_suffix_intercepts_all_window_words():
    """The regex matches last/in/within/past/previous + day/week/month/year."""
    for col in (
        "payments_in_30_days",
        "users_active_within_60_days",
        "orders_past_2_weeks",
        "signups_previous_3_months",
        "events_last_5_years",
    ):
        got = of._detect_col_type(col, [42])
        assert got == "count", f"{col} must classify as count, got {got}"


def test_legitimate_days_columns_unaffected_by_s3_fix():
    """The fix must not break columns that are genuinely durations."""
    for col in (
        "processing_time_days",
        "days_since_created",
        "lag_days",
        "turnaround_days",
        "duration_days",
        "elapsed_days",
    ):
        got = of._detect_col_type(col, [7])
        assert got == "days", f"{col} must still classify as days, got {got}"


# ── _detect_col_type: full type spread ───────────────────────────────

def test_detect_col_type_date_from_datetime_value():
    assert of._detect_col_type("createdAt", [datetime(2026, 5, 20)]) == "date"


def test_detect_col_type_date_from_iso_string():
    assert of._detect_col_type("createdAt", ["2026-05-20 04:01:23"]) == "date"


def test_detect_col_type_currency_from_unambiguous_keyword():
    assert of._detect_col_type("payment_amount", [Decimal("12345.67")]) == "currency"


def test_detect_col_type_percentage_from_keyword():
    assert of._detect_col_type("win_rate", [0.65]) == "percentage"


def test_detect_col_type_raw_for_unknown_string():
    assert of._detect_col_type("status_label", ["OPEN"]) == "raw"


def test_detect_col_type_bool_is_raw():
    assert of._detect_col_type("paid_in_full", [True]) == "raw"


# ── Formatters ───────────────────────────────────────────────────────

def test_fmt_currency_us_locale():
    assert of._fmt_currency(1234.5, {"locale": "US"}) == "$1,234.50"


def test_fmt_currency_no_cents_preference():
    """currency_no_cents drops the decimal portion.

    Uses 1234.6 (not .5) to avoid Python's round-half-to-even behaviour
    on banker's rounding — the goal of the test is the format shape,
    not the rounding mode.
    """
    out = of._fmt_currency(1234.6, {"locale": "US", "currency_no_cents": True})
    assert out == "$1,235", f"Rounded no-cents form expected, got {out!r}"


def test_fmt_currency_eu_locale_swaps_separators():
    """EU uses '.' for thousands and ',' for decimals."""
    out = of._fmt_currency(1234.5, {"locale": "EU"})
    assert out == "$1.234,50", f"EU locale separator swap failed, got {out!r}"


def test_fmt_currency_handles_decimal_input():
    assert of._fmt_currency(Decimal("99.99"), {"locale": "US"}) == "$99.99"


def test_fmt_date_strips_leading_zero():
    """Day '05' renders as '5' (no leading zero) per the US/UK/EU formats."""
    out = of._fmt_date(datetime(2026, 5, 5), {"locale": "US"})
    assert "May 5, 2026" == out or out.startswith("May 5"), out


def test_fmt_days_thousands_separator():
    assert of._fmt_days(1234) == "1,234 days"


def test_fmt_count_thousands_separator():
    assert of._fmt_count(1020) == "1,020"
    assert of._fmt_count("not-a-number") == "not-a-number"


def test_fmt_percentage_autoscales_0_to_1_to_percent():
    """A column with sample values in [0, 1] is treated as fractions."""
    out = of._fmt_percentage(0.65, "win_rate", [0.1, 0.5, 0.8])
    assert out == "65.00%"


def test_fmt_percentage_passes_through_already_in_percent():
    """Sample values > 1 → treat value as already a percent."""
    out = of._fmt_percentage(45.5, "pct_change", [10.0, 20.0, 45.5])
    assert out == "45.50%"


# ── Conditional formatting ───────────────────────────────────────────

def test_conditional_negative_currency_is_red():
    style = of._compute_conditional_format("balance", "currency", -100, "-$100.00")
    assert style.get("color") == "#E74C3C"


def test_conditional_urgency_overdue_is_red_bold():
    style = of._compute_conditional_format("due_date_urgency", "raw", "overdue", "overdue")
    assert style.get("color") == "#E74C3C"
    assert style.get("font-weight") == "bold"


def test_conditional_paid_in_full_true_is_green():
    style = of._compute_conditional_format("paid_in_full_ip", "raw", True, "True")
    assert style.get("color") == "#27AE60"


def test_conditional_pct_change_positive_is_green():
    style = of._compute_conditional_format("revenue_pct_change", "raw", "+15.5%", "+15.5%")
    assert style.get("color") == "#27AE60"


def test_conditional_returns_empty_for_normal_values():
    style = of._compute_conditional_format("name", "raw", "Alice", "Alice")
    assert style == {}


# ── _format_rows end-to-end ──────────────────────────────────────────

def test_format_rows_applies_per_column_types():
    rows = [
        {"createdAt": datetime(2026, 5, 20), "balance": Decimal("12.50"), "name": "Org A"},
        {"createdAt": datetime(2026, 5, 21), "balance": Decimal("-7.25"), "name": "Org B"},
    ]
    formatted, col_types, cell_styles = of._format_rows(rows, {"locale": "US"})
    assert col_types["createdAt"] == "date"
    assert col_types["balance"] == "currency"
    assert col_types["name"] == "raw"
    assert formatted[0]["balance"] == "$12.50"
    assert formatted[1]["balance"] == "-$7.25"
    # Negative balance must get a red cell style
    assert "1:balance" in cell_styles
    assert cell_styles["1:balance"]["color"] == "#E74C3C"


def test_format_rows_handles_none_values_gracefully():
    rows = [{"x": None, "y": 5}, {"x": 1.0, "y": None}]
    formatted, _, _ = of._format_rows(rows, {})
    assert formatted[0]["x"] is None
    assert formatted[1]["y"] is None


# ── Node-level ───────────────────────────────────────────────────────

def test_output_formatter_node_returns_unchanged_state_on_empty_rows():
    state = {"query_result": [], "agent_trace": [], "user_preferences": {}}
    out = of.output_formatter_node(state)
    # No formatting performed; state passed through
    assert out["query_result"] == []


def test_output_formatter_node_writes_cell_styles_and_trace():
    state = {
        "query_result": [
            {"shortId": "ABC", "amount": Decimal("100.00")},
            {"shortId": "DEF", "amount": Decimal("-50.00")},
        ],
        "user_preferences": {"locale": "US"},
        "agent_trace": [],
    }
    out = of.output_formatter_node(state)
    rows = out["query_result"]
    # Currency formatting applied to amount
    assert rows[0]["amount"] == "$100.00"
    assert rows[1]["amount"] == "-$50.00"
    # Trace entry created
    last_trace = out["agent_trace"][-1]
    assert last_trace["agent"] == "Output Formatter"
    assert "Formatted" in last_trace["summary"] or "formatting" in last_trace["summary"].lower()
    # cell_styles emitted for the negative amount
    assert "cell_styles" in out
    assert len(out["cell_styles"]) >= 1


# ── Capability pin ───────────────────────────────────────────────────

def test_public_surface_preserved():
    for name in (
        "output_formatter_node",
        "_format_rows",
        "_detect_col_type",
        "_compute_conditional_format",
        "_fmt_currency",
        "_fmt_date",
        "_fmt_days",
        "_fmt_count",
        "_fmt_percentage",
        "_tokenize",
        "_LOCALE_CONFIGS",
        "_DAYS_KEYWORDS",
        "_PERCENTAGE_KEYWORDS",
        "_COUNT_KEYWORDS",
        "_TIME_WINDOW_SUFFIX_RE",  # Node 10 addition
    ):
        assert hasattr(of, name), f"Public surface lost: {name}"
