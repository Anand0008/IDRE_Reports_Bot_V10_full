"""Tests for agents/post_processor.py (Node 9).

Pins the value-mutating behaviour of the 10 sub-processors + the
Node 9 cleanups: V10 header, DST-aware America/New_York timezone,
dead-code removal (user_transformations + orphan helpers + dead
__table branch), externalized urgency_thresholds.

The post-processor mutates `state['query_result']` rows directly,
so each sub-processor test asserts row content rather than agent state.
"""
import inspect
import json
from datetime import datetime, timedelta
from decimal import Decimal

import agents.post_processor as pp


# ── Phase 1 (I1): V10 header refresh ─────────────────────────────────

def test_module_header_refreshed_to_v10():
    doc = pp.__doc__ or ""
    assert "Post-SQL Processor Agent — V10" in doc, "Header must say V10"
    # The stale "V6" header line is gone
    first_line = doc.strip().splitlines()[0]
    assert "V6" not in first_line, f"First header line still references V6: {first_line!r}"
    assert "History:" in doc, "History subsection must be present"
    assert "V3/V4" in doc and "V6" in doc and "V10" in doc, (
        "History must span V3/V4 → V6 → V10"
    )


# ── Phase 2 (I3 + M5): DST-aware EST + _urgency_level default ────────

def test_now_est_returns_naive_eastern_time():
    """_now_est() returns a naive datetime in America/New_York wall-clock.
    Naive so callers using naive _to_date outputs don't mix aware+naive."""
    now = pp._now_est()
    assert now.tzinfo is None, "_now_est() must return naive datetime"


def test_urgency_level_default_now_uses_eastern_not_local():
    """Smoke for M5: when `now` is omitted, _urgency_level falls back to
    _now_est() (America/New_York) — NOT datetime.now() local time. Verify
    by patching _now_est and confirming the default is taken from there."""
    sentinel = datetime(2026, 6, 15, 12, 0, 0)
    # Patch _now_est to return our sentinel; if _urgency_level uses local
    # datetime.now() the result will be wildly different.
    import agents.post_processor as pp_mod
    orig = pp_mod._now_est
    pp_mod._now_est = lambda: sentinel
    try:
        # Due 5 days after the sentinel → "normal"
        out = pp._urgency_level(sentinel + timedelta(days=5))
        assert out["urgency"] == "normal"
        assert out["days"] == 5
    finally:
        pp_mod._now_est = orig


def test_zoneinfo_dst_handles_summer_and_winter():
    """The DST fix relies on zoneinfo.ZoneInfo('America/New_York').
    Confirm summer (EDT, UTC-4) and winter (EST, UTC-5) both work."""
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("America/New_York")

    # Summer (EDT, UTC-4)
    summer = datetime(2026, 7, 15, 12, 0, 0, tzinfo=tz)
    assert summer.utcoffset() == timedelta(hours=-4), (
        f"July must be EDT (UTC-4), got {summer.utcoffset()}"
    )
    # Winter (EST, UTC-5)
    winter = datetime(2026, 1, 15, 12, 0, 0, tzinfo=tz)
    assert winter.utcoffset() == timedelta(hours=-5), (
        f"January must be EST (UTC-5), got {winter.utcoffset()}"
    )


# ── Phase 3 (M1 + M2 + M4): dead code removed ────────────────────────

def test_dead_user_transformations_removed():
    """user_transformations dead-on-arrival pattern removed in Node 9."""
    assert not hasattr(pp, "_apply_user_transformations"), (
        "_apply_user_transformations must be removed (file never existed)"
    )
    assert not hasattr(pp, "_load_user_transformations"), (
        "_load_user_transformations must be removed"
    )
    assert not hasattr(pp, "_USER_TRANSFORMS_PATH")


def test_orphan_helpers_removed():
    """V6-era helpers defined but never called in V10."""
    assert not hasattr(pp, "_business_days_between"), "Orphan _business_days_between must be removed"
    assert not hasattr(pp, "_add_business_days"), "Orphan _add_business_days must be removed"
    assert not hasattr(pp, "_expected_fee"), (
        "Orphan _expected_fee must be removed (_process_expected_fees inlines pricing)"
    )


def test_soft_delete_filter_no_dead_table_branch():
    """The `row.get('__table')` branch was structurally unreachable
    (SQLAlchemy dict rows have no __table key). Removed in Node 9.

    We assert the specific dead-call pattern is gone from the function
    body — the docstring may still describe the history (which is
    intentional documentation, not dead code)."""
    source = inspect.getsource(pp._process_soft_delete_filter)
    # The dead pattern was `row.get("__table"` — that's what must not exist.
    assert 'row.get("__table"' not in source, (
        "Dead `row.get('__table')` call must be removed"
    )
    assert "row.get('__table'" not in source, (
        "Dead row.get('__table') call must be removed (single-quote variant)"
    )


def test_soft_delete_filter_still_removes_status_removed_dispute_line_items():
    """Defence-in-depth path preserved — sql_writer also mandates ACTIVE
    upstream, but if it regresses, this catches it."""
    rows = [
        {"disputeName": "X1", "status": "ACTIVE"},
        {"disputeName": "X2", "status": "REMOVED"},   # should be dropped
        {"disputeName": "X3", "status": "ACTIVE"},
    ]
    out = pp._process_soft_delete_filter(rows)
    assert len(out) == 2
    statuses = {r["status"] for r in out}
    assert "REMOVED" not in statuses


# ── Phase 4 (M3): urgency_thresholds externalized ────────────────────

def test_urgency_thresholds_read_from_business_rules():
    """The block in business_rules.json must drive the urgency boundaries."""
    t = pp._get_urgency_thresholds()
    assert isinstance(t, dict)
    assert "urgent_days" in t and "warning_days" in t
    assert isinstance(t["urgent_days"], int) and isinstance(t["warning_days"], int)


def test_urgency_thresholds_tunable_via_config(monkeypatch):
    """Confirm that changing the loaded business_rules changes the boundary."""
    fake_rules = {"urgency_thresholds": {"urgent_days": 5, "warning_days": 10}}
    monkeypatch.setattr(pp, "_load_business_rules", lambda: fake_rules)
    # Bust the rules cache too since _load_business_rules normally caches
    monkeypatch.setattr(pp, "_rules_cache", None)
    t = pp._get_urgency_thresholds()
    assert t["urgent_days"] == 5
    assert t["warning_days"] == 10

    # Behaviour test: a date 4 days away should now classify as "urgent"
    # (was "warning" with the default urgent_days=1)
    now = datetime(2026, 5, 20)
    out = pp._urgency_level(now + timedelta(days=4), now)
    assert out["urgency"] == "urgent", (
        f"With urgent_days=5, a 4-day-out date should classify urgent, got {out['urgency']}"
    )


# ── Helpers: _to_number, _to_date ────────────────────────────────────

def test_to_number_handles_common_inputs():
    assert pp._to_number(42) == 42.0
    assert pp._to_number(3.14) == 3.14
    assert pp._to_number(Decimal("9.99")) == 9.99
    assert pp._to_number("1,234.56") == 1234.56
    assert pp._to_number("not a number") is None
    assert pp._to_number(None) is None


def test_to_date_parses_iso_and_objects():
    dt = pp._to_date("2026-05-20 04:01:23")
    assert isinstance(dt, datetime)
    assert dt.year == 2026 and dt.month == 5 and dt.day == 20
    # date object
    from datetime import date as date_cls
    out = pp._to_date(date_cls(2026, 5, 20))
    assert isinstance(out, datetime)
    # Invalid string returns None
    assert pp._to_date("not-a-date") is None
    assert pp._to_date(None) is None


# ── Sub-processors ───────────────────────────────────────────────────

def test_process_dispute_numbers_from_shortId():
    rows = [{"shortId": "JB4XBW8"}, {"shortId": "WYUUABG"}]
    out = pp._process_dispute_numbers(rows)
    assert out[0]["dispute_number"] == "DISP-JB4XBW8"
    assert out[1]["dispute_number"] == "DISP-WYUUABG"


def test_process_dispute_numbers_idempotent_on_prefixed():
    rows = [{"dispute_number": "DISP-ALREADY"}]
    out = pp._process_dispute_numbers(rows)
    assert out[0]["dispute_number"] == "DISP-ALREADY", "Must not double-prefix"


def test_process_cents_to_dollars_camel_and_snake():
    rows = [
        {"refundAmountCents": 12345, "fee_cents": 99},
        {"refundAmountCents": 0, "fee_cents": None},
    ]
    out = pp._process_cents_to_dollars(rows)
    assert out[0]["refundAmount_dollars"] == 123.45
    assert out[0]["fee_dollars"] == 0.99
    assert out[1]["refundAmount_dollars"] == 0.0
    # None should leave no _dollars key (skipped)
    assert "fee_dollars" not in out[1]


def test_process_urgency_scoring_classifies_due_date(monkeypatch):
    """Pin _now_est() to a deterministic anchor so day-boundary arithmetic
    doesn't depend on the test's wall-clock fractional seconds."""
    anchor = datetime(2026, 5, 20, 12, 0, 0)
    monkeypatch.setattr(pp, "_now_est", lambda: anchor)

    rows = [
        {"due_date": (anchor - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")},  # overdue
        {"due_date": (anchor + timedelta(hours=6)).strftime("%Y-%m-%d %H:%M:%S")}, # same day → urgent
        {"due_date": (anchor + timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")},  # warning
        {"due_date": (anchor + timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")}, # normal
    ]
    out = pp._process_urgency_scoring(rows)
    urgencies = [r["due_date_urgency"] for r in out]
    assert urgencies[0] == "overdue"
    assert urgencies[1] == "urgent"
    assert urgencies[2] == "warning"
    assert urgencies[3] == "normal"


def test_process_processing_time_calendar_days():
    rows = [{
        "createdAt": "2026-05-10 09:00:00",
        "statusChangedAt": "2026-05-20 09:00:00",
    }]
    out = pp._process_processing_time(rows)
    assert out[0]["processing_time_days"] == 10


def test_process_processing_time_clamps_negative_to_zero():
    """If statusChangedAt is somehow before createdAt, clamp to 0."""
    rows = [{
        "createdAt": "2026-05-20",
        "statusChangedAt": "2026-05-10",
    }]
    out = pp._process_processing_time(rows)
    assert out[0]["processing_time_days"] == 0


def test_process_payment_status_labels_from_config():
    rows = [
        {"direction": "INCOMING", "type": "CASE_PAYMENT"},
        {"direction": "OUTGOING", "type": "PARTY_REFUND_IP"},
        {"direction": "UNKNOWN_DIRECTION", "type": "UNKNOWN_TYPE"},  # unmapped → passthrough
    ]
    out = pp._process_payment_status(rows)
    assert out[0]["direction_label"] == "Received"
    assert out[0]["payment_type_label"] == "Case Fee"
    assert out[1]["direction_label"] == "Sent"
    assert out[1]["payment_type_label"] == "IP Refund"
    # Unmapped fall through to their original values
    assert out[2]["direction_label"] == "UNKNOWN_DIRECTION"


def test_process_expected_fees_single_dispute():
    rows = [{
        "disputeType": "SINGLE",
        "createdAt": "2026-05-20",
    }]
    out = pp._process_expected_fees(rows)
    assert out[0]["expected_entity_fee"] == 595.0
    assert out[0]["expected_cms_fee"] == 115.0
    assert out[0]["expected_total_fee"] == 710.0
    assert out[0]["pricing_era"] == "current"


def test_process_expected_fees_batched_with_surcharge():
    """BATCHED with > 25 line items → +$150 per extra group of 25."""
    rows = [{
        "disputeType": "BATCHED",
        "createdAt": "2026-05-20",
        "line_item_count": 30,  # 5 above 25 → 1 surcharge group
    }]
    out = pp._process_expected_fees(rows)
    assert out[0]["expected_total_fee"] == 910.0 + 150.0


def test_process_payment_variance_descriptions():
    rows = [
        {"varianceType": "EXACT"},
        {"varianceType": "OVERPAYMENT", "varianceAmount": 25.50},
        {"varianceType": "UNDERPAYMENT", "varianceAmount": -10.00},
    ]
    out = pp._process_payment_variance(rows)
    assert out[0]["variance_description"] == "Paid exact amount"
    assert "Overpaid by $25.50" in out[1]["variance_description"]
    assert "Underpaid by $10.00" in out[2]["variance_description"]


def test_process_paid_in_full_flags():
    rows = [
        {"disputeType": "SINGLE", "createdAt": "2026-05-20",
         "ip_paid_amount": 710.0, "nip_paid_amount": 500.0},
        {"disputeType": "SINGLE", "createdAt": "2026-05-20",
         "ip_paid_amount": 710.0, "nip_paid_amount": 710.0},
    ]
    out = pp._process_paid_in_full(rows)
    assert out[0]["paid_in_full_ip"] is True
    assert out[0]["paid_in_full_nip"] is False
    assert out[0]["both_paid_in_full"] is False
    assert out[1]["both_paid_in_full"] is True


def test_process_historical_comparison_pct_change():
    rows = [
        {"month": "2026-01", "revenue": 100},
        {"month": "2026-02", "revenue": 150},  # +50%
        {"month": "2026-03", "revenue": 120},  # -20%
    ]
    out = pp._process_historical_comparison(rows)
    assert out[0]["revenue_pct_change"] is None  # first row has no prior
    assert out[1]["revenue_pct_change"] == "+50.0%"
    assert out[2]["revenue_pct_change"] == "-20.0%"


# ── Processor detection ──────────────────────────────────────────────

def test_detect_needed_processors_picks_right_subset():
    rows = [{"shortId": "ABC", "refundAmountCents": 100}]
    procs = pp._detect_needed_processors("anything", "SELECT * FROM `case`", rows)
    assert "dispute_numbers" in procs
    assert "cents_to_dollars" in procs
    # No urgency, processing_time, payment_status, etc. for this row shape
    assert "urgency_scoring" not in procs
    # user_transformations was removed in Node 9
    assert "user_transformations" not in procs


def test_detect_needed_processors_empty_rows_returns_minimal():
    """Empty result set → no shape-based processors triggered."""
    procs = pp._detect_needed_processors("q", "SELECT 1", [])
    # user_transformations gone; no shape ⇒ procs should be empty or near-empty
    assert "dispute_numbers" not in procs
    assert "user_transformations" not in procs


# ── Node-level ───────────────────────────────────────────────────────

def test_post_processor_node_skips_on_empty_rows():
    state = {
        "query_result": [],
        "agent_trace": [],
        "resolved_query": "anything",
        "validated_sql": "SELECT 1",
        "user_query": "anything",
    }
    out = pp.post_processor_node(state)
    last_trace = out["agent_trace"][-1]
    assert last_trace["agent"] == "Post-Processor"
    assert "skipped" in last_trace["summary"].lower() or "no rows" in last_trace["summary"].lower()


def test_post_processor_node_enriches_and_documents_columns():
    state = {
        "query_result": [{"shortId": "ABC123", "refundAmountCents": 12345}],
        "agent_trace": [],
        "resolved_query": "show me cases",
        "validated_sql": "SELECT shortId, refundAmountCents FROM `case`",
        "user_query": "show me cases",
    }
    out = pp.post_processor_node(state)
    rows = out["query_result"]
    assert rows[0]["dispute_number"] == "DISP-ABC123"
    assert rows[0]["refundAmount_dollars"] == 123.45
    # Tooltips populated
    assert "computed_column_tooltips" in out
    assert "dispute_number" in out["computed_column_tooltips"]


# ── Capability pin ───────────────────────────────────────────────────

def test_public_surface_preserved():
    """Capability pin — refactors must preserve these names."""
    for name in (
        "post_processor_node",
        "_detect_needed_processors",
        "_process_dispute_numbers",
        "_process_cents_to_dollars",
        "_process_urgency_scoring",
        "_process_processing_time",
        "_process_payment_status",
        "_process_soft_delete_filter",
        "_process_expected_fees",
        "_process_payment_variance",
        "_process_paid_in_full",
        "_process_historical_comparison",
        "_urgency_level",
        "_get_urgency_thresholds",
        "_now_est",
        "_to_number",
        "_to_date",
        "COMPUTED_COLUMN_DOCS",
        "TERMINAL_STATUSES",
    ):
        assert hasattr(pp, name), f"Public surface lost: {name}"
