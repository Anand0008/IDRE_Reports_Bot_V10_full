"""
Post-SQL Processor Agent — V10

Runs AFTER the executor. Adds IDRE-specific computed columns to the
raw result rows. The post-processor decides which sub-processors to
run via `_detect_needed_processors(query, sql, rows)` — it inspects
the row shape rather than running everything blindly.

Ten sub-processors:
  - dispute_numbers       — shortId → DISP-XXXXXXX
  - cents_to_dollars      — any `*Cents` / `*_cents` column → `*_dollars`
                            (value / 100, rounded to 2 dp)
  - urgency_scoring       — due_date / eligibilityDueDate / paymentDueDate
                            → urgency level + message + days_remaining,
                            DST-aware America/New_York timezone
  - processing_time       — createdAt → statusChangedAt calendar days
  - payment_status        — direction + type → human-readable labels
                            from config/business_rules.json
  - soft_delete_filter    — defence-in-depth removal of status=REMOVED
                            rows (sql_writer also mandates this upstream)
  - expected_fees         — entity/cms/total fee + pricing_era
                            (era-aware via pricing_era_boundaries)
  - payment_variance      — variance_type + amount → "Paid exact" /
                            "Overpaid by $X" / "Underpaid by $X"
  - paid_in_full          — ip_paid/nip_paid → flags + status strings
                            (era-aware $710 SINGLE/BUNDLED, $910 BATCHED)
  - historical_comparison — period-over-period % change for time-series
                            results (regex-detected period column)

Computed-column documentation flows into state['computed_column_tooltips']
via `_collect_column_tooltips()` against `COMPUTED_COLUMN_DOCS`.

History:
- V3/V4: agent introduced — base sub-processors (dispute_number,
  cents-to-dollars, urgency scoring, payment labels).
- V5+: byte-identical to V3/V4 (stability).
- V6: configurable business rules (config/business_rules.json),
  computed-column tooltip metadata, pricing-era versioning,
  user-defined transformations (data/user_transformations.json).
- V7/V8/V9: byte-identical to V6 (four versions of architectural
  stability).
- V10 (May 17): @trace_agent("v10.agent.post_processor") decorator +
  tracing import; no behavioural change vs V9.
- V10 Node 9 (May 20): module header refreshed; switched to
  `zoneinfo.ZoneInfo("America/New_York")` for DST-aware urgency
  scoring (previously a fixed -5h offset that was wrong 8 months/
  year); removed the user_transformations dead-on-arrival path
  (data/user_transformations.json never existed in any version) +
  three orphan helpers (_business_days_between, _add_business_days,
  _expected_fee — all defined but never called); externalized
  urgency_thresholds reads from business_rules.json; stripped the
  dead `__table` branch from _process_soft_delete_filter and
  documented its defence-in-depth role. See
  local/docs/superpowers/reports/2026-05-20-node9-post-processor-audit.md.
"""
import re
import json
import math
import os
from datetime import datetime, date
from decimal import Decimal
from typing import Any, Optional
from zoneinfo import ZoneInfo
from state.context import GraphState
from tracing import trace_agent

# Node 9 (2026-05-20): DST-aware America/New_York timezone replaces the
# old fixed -5h offset. UTC-5 (EST) is correct only Nov-Mar; mid-Mar
# through early-Nov is UTC-4 (EDT). The previous fixed offset shifted
# "due tomorrow" → "due today" across the day boundary 8 months/year.
_EASTERN_TZ = ZoneInfo("America/New_York")

_CONFIG_DIR = os.path.join(os.path.dirname(__file__), "..", "config")
_BUSINESS_RULES_PATH = os.path.join(_CONFIG_DIR, "business_rules.json")

_rules_cache = None


def _load_business_rules() -> dict:
    global _rules_cache
    if _rules_cache is not None:
        return _rules_cache
    try:
        with open(_BUSINESS_RULES_PATH, encoding="utf-8") as f:
            _rules_cache = json.load(f)
    except (OSError, json.JSONDecodeError):
        _rules_cache = {}
    return _rules_cache


def _get_pricing(era: str = "current") -> dict:
    rules = _load_business_rules()
    pricing_versions = rules.get("pricing_versions", {})
    if era in pricing_versions:
        return pricing_versions[era]
    return pricing_versions.get("current", {
        "SINGLE": {"entity_fee": 595.00, "cms_fee": 115.00, "total": 710.00},
        "BUNDLED": {"entity_fee": 595.00, "cms_fee": 115.00, "total": 710.00},
        "BATCHED": {"entity_fee": 795.00, "cms_fee": 115.00, "base_total": 910.00,
                    "surcharge_per_25": 150.00},
    })


def _determine_pricing_era(row: dict) -> str:
    created = _to_date(row.get("createdAt") or row.get("created_at"))
    if not created:
        return "current"
    rules = _load_business_rules()
    era_boundaries = rules.get("pricing_era_boundaries", [])
    for boundary in sorted(era_boundaries, key=lambda b: b.get("effective_date", ""), reverse=True):
        eff = _to_date(boundary.get("effective_date"))
        if eff and created >= eff:
            return boundary.get("era_name", "current")
    return "current"


TERMINAL_STATUSES = {
    "CLOSED_DEFAULT", "CLOSED_INITIATING_PARTY", "CLOSED_NON_INITIATING_PARTY",
    "CLOSED_ADMINISTRATIVE", "CLOSED_SPLIT_DECISION",
    "NOTICE_OF_DISMISSAL_NON_PAYMENT", "CLOSED_DEFAULT_IP", "CLOSED_DEFAULT_NIP",
    "INELIGIBLE",
}

REFUND_CENTS_COLUMNS = {"refundAmountCents", "refund_amount_cents"}

COMPUTED_COLUMN_DOCS = {
    "dispute_number": "Formatted dispute ID: DISP-XXXXXXX (from shortId)",
    "_dollars": "Converted from cents to dollars (value / 100)",
    "_urgency": "Urgency level: overdue (past due), urgent (0-1 days), warning (2-3 days), normal (4+ days). Uses EST timezone.",
    "_message": "Human-readable urgency description",
    "_days_remaining": "Calendar days until due date (negative = overdue)",
    "processing_time_days": "Calendar days between creation and status change/closure",
    "direction_label": "Human-readable payment direction: Received/Sent",
    "payment_type_label": "Human-readable payment type: Case Fee, Party Refund, etc.",
    "expected_entity_fee": "Expected entity fee based on dispute type and pricing era",
    "expected_cms_fee": "Expected CMS fee based on dispute type",
    "expected_total_fee": "Expected total fee (entity + CMS + surcharges for BATCHED)",
    "variance_description": "Payment variance: Paid exact amount / Overpaid by $X / Underpaid by $X",
    "paid_in_full_ip": "Boolean: Initiating Party paid >= threshold ($710 SINGLE/BUNDLED, $910 BATCHED)",
    "paid_in_full_nip": "Boolean: Non-Initiating Party paid >= threshold",
    "both_paid_in_full": "Boolean: Both IP and NIP have paid in full",
    "ip_payment_status": "IP payment status: 'Paid in full' or '$X of $Y'",
    "nip_payment_status": "NIP payment status: 'Paid in full' or '$X of $Y'",
    "_pct_change": "Period-over-period percentage change vs previous row",
}


def _to_number(val: Any) -> Optional[float]:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, Decimal):
        return float(val)
    if isinstance(val, str):
        try:
            return float(val.replace(",", ""))
        except ValueError:
            return None
    return None


def _to_date(val: Any) -> Optional[datetime]:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val
    if isinstance(val, date):
        return datetime.combine(val, datetime.min.time())
    if isinstance(val, str):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d",
                     "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d %H:%M:%S.%f"):
            try:
                return datetime.strptime(val[:26], fmt)
            except (ValueError, IndexError):
                continue
    return None


# Node 9 (2026-05-20): removed orphan helpers _add_business_days +
# _business_days_between — defined but never called in V10. The V6
# era's "expected close date" computation that used them was replaced
# by the simpler processing_time_days calendar-day calculation.


def _get_urgency_thresholds() -> dict:
    """Read urgency thresholds from business_rules.json (or fall back to defaults).

    Node 9 (2026-05-20): externalized so non-engineers can tune the
    overdue/urgent/warning cut-offs without code change. The JSON
    block has existed in business_rules.json since V6 but was previously
    ignored — the values were hardcoded.

    Default thresholds match the prior hardcoded behaviour:
      overdue: diff < 0 (i.e. past due_date)
      urgent:  diff <= urgent_days (default 1) — today or tomorrow
      warning: diff <= warning_days (default 3)
      normal:  diff > warning_days
    """
    rules = _load_business_rules()
    cfg = rules.get("urgency_thresholds") or {}
    return {
        "urgent_days":  int(cfg.get("urgent_days", 1)),
        "warning_days": int(cfg.get("warning_days", 3)),
    }


def _urgency_level(due_date: datetime, now: Optional[datetime] = None) -> dict:
    # Node 9 (2026-05-20): default falls back to _now_est() instead of
    # datetime.now() (local time) so any direct caller gets the same
    # America/New_York semantics as _process_urgency_scoring's invocation.
    now = now or _now_est()
    diff = (due_date - now).days
    thresholds = _get_urgency_thresholds()

    if diff < 0:
        return {"urgency": "overdue", "days": abs(diff),
                "message": f"{abs(diff)} day(s) overdue"}
    elif diff <= thresholds["urgent_days"]:
        return {"urgency": "urgent", "days": diff,
                "message": f"Due {'today' if diff == 0 else 'tomorrow'}"}
    elif diff <= thresholds["warning_days"]:
        return {"urgency": "warning", "days": diff,
                "message": f"Due in {diff} days"}
    else:
        return {"urgency": "normal", "days": diff,
                "message": f"Due in {diff} days"}


# Node 9 (2026-05-20): removed orphan helper _expected_fee — defined
# but never called. _process_expected_fees inlines the same pricing
# math directly against config/business_rules.json.


def _now_est() -> datetime:
    """Current wall-clock time in America/New_York (DST-aware).

    Returns a naive datetime so callers using naive arithmetic with
    `_to_date()` outputs (also naive) don't accidentally mix
    aware + naive (which raises TypeError).
    """
    return datetime.now(_EASTERN_TZ).replace(tzinfo=None)


# Node 9 (2026-05-20): removed _load_user_transformations +
# _apply_user_transformations + _USER_TRANSFORMS_PATH. They read from
# `data/user_transformations.json` which has never existed on disk in
# any version (V3/V4 through V10) — same dead-on-arrival pattern as
# Node 5's `column_usage.json`. ~60 LOC of code that ran every request
# but did nothing.


def _process_dispute_numbers(rows: list[dict]) -> list[dict]:
    for row in rows:
        if "dispute_number" in row and row["dispute_number"]:
            val = str(row["dispute_number"])
            if not val.startswith("DISP-"):
                row["dispute_number"] = f"DISP-{val}"
        elif "shortId" in row and row["shortId"]:
            row["dispute_number"] = f"DISP-{row['shortId']}"
    return rows


def _process_cents_to_dollars(rows: list[dict]) -> list[dict]:
    for row in rows:
        for key in list(row.keys()):
            if key.endswith("Cents") or key.endswith("_cents"):
                val = _to_number(row[key])
                if val is not None:
                    dollar_key = key.replace("Cents", "").replace("_cents", "") + "_dollars"
                    row[dollar_key] = round(val / 100, 2)
    return rows


def _process_urgency_scoring(rows: list[dict]) -> list[dict]:
    now = _now_est()
    for row in rows:
        for key in ["due_date", "dueDate", "primary_due_date", "eligibilityDueDate",
                     "paymentDueDate", "due_date_until_decision"]:
            if key in row and row[key]:
                dt = _to_date(row[key])
                if dt:
                    urgency = _urgency_level(dt, now)
                    row[f"{key}_urgency"] = urgency["urgency"]
                    row[f"{key}_message"] = urgency["message"]
                    row[f"{key}_days_remaining"] = urgency["days"]
    return rows


def _process_processing_time(rows: list[dict]) -> list[dict]:
    for row in rows:
        created = _to_date(row.get("createdAt") or row.get("created_at"))
        changed = _to_date(row.get("statusChangedAt") or row.get("status_changed_at")
                           or row.get("closed_at"))
        if created and changed:
            diff = (changed - created).days
            row["processing_time_days"] = max(diff, 0)
    return rows


def _process_payment_status(rows: list[dict]) -> list[dict]:
    rules = _load_business_rules()
    direction_labels = rules.get("direction_labels", {
        "INCOMING": "Received", "OUTGOING": "Sent",
    })
    type_labels = rules.get("payment_type_labels", {
        "CASE_PAYMENT": "Case Fee",
        "REFUND_TO_PREVAILING_PARTY": "Party Refund",
        "PARTY_REFUND_IP": "IP Refund",
        "PARTY_REFUND_NIP": "NIP Refund",
        "CAPITOL_BRIDGE_FEE": "Capitol Bridge Fee",
        "THIRD_PARTY_PAYMENT": "Internal Payout",
        "CMS_INVOICE_PAYMENT": "CMS Payment",
        "CMS_ADMIN_FEE_TRANSFER": "CMS Admin Fee",
    })
    for row in rows:
        if "direction" in row:
            row["direction_label"] = direction_labels.get(row["direction"], row["direction"])
        pay_type = row.get("type") or row.get("paymentType")
        if pay_type:
            row["payment_type_label"] = type_labels.get(pay_type, pay_type)
    return rows


def _process_soft_delete_filter(rows: list[dict]) -> list[dict]:
    """Defence-in-depth soft-delete filter for dispute_line_items.

    sql_writer's V10 SYSTEM_PROMPT (rule R / DISPLAY RULES) already
    mandates `dispute_line_items WHERE status = 'ACTIVE'`. This post-
    execution check is the belt-and-suspenders safety net for the
    case where the LLM ignores that rule or a future template skips
    the filter. Drops rows whose `status` is `REMOVED` AND whose row
    shape (presence of `disputeName`) indicates dispute_line_items.

    Node 9 (2026-05-20): removed the dead `__table` heuristic branch
    (SQLAlchemy dict rows never carry a `__table` key, so the branch
    was structurally unreachable).
    """
    filtered = []
    for row in rows:
        if row.get("status") == "REMOVED" and "disputeName" in row:
            continue
        filtered.append(row)
    return filtered if filtered else rows


def _process_expected_fees(rows: list[dict]) -> list[dict]:
    for row in rows:
        dtype = row.get("disputeType") or row.get("dispute_type") or row.get("typeOfDispute")
        if dtype:
            dtype_upper = str(dtype).upper()
            line_count = _to_number(row.get("line_item_count") or row.get("lineItemCount") or 1) or 1
            era = _determine_pricing_era(row)
            pricing = _get_pricing(era)
            if dtype_upper == "BATCHED":
                batched = pricing.get("BATCHED", {})
                base = batched.get("base_total", 910.0)
                if line_count > 25:
                    extra_groups = math.ceil((line_count - 25) / 25)
                    total = base + extra_groups * batched.get("surcharge_per_25", 150.0)
                else:
                    total = base
                row["expected_entity_fee"] = batched.get("entity_fee", 795.0)
                row["expected_cms_fee"] = batched.get("cms_fee", 115.0)
                row["expected_total_fee"] = total
            elif dtype_upper in pricing:
                row["expected_entity_fee"] = pricing[dtype_upper].get("entity_fee", 595.0)
                row["expected_cms_fee"] = pricing[dtype_upper].get("cms_fee", 115.0)
                row["expected_total_fee"] = pricing[dtype_upper].get("total", 710.0)
            row["pricing_era"] = era
    return rows


def _process_payment_variance(rows: list[dict]) -> list[dict]:
    for row in rows:
        vtype = row.get("varianceType") or row.get("variance_type")
        vamount = _to_number(row.get("varianceAmount") or row.get("variance_amount"))
        if vtype:
            if vtype == "EXACT":
                row["variance_description"] = "Paid exact amount"
            elif vtype == "OVERPAYMENT" and vamount is not None:
                row["variance_description"] = f"Overpaid by ${abs(vamount):,.2f}"
            elif vtype == "UNDERPAYMENT" and vamount is not None:
                row["variance_description"] = f"Underpaid by ${abs(vamount):,.2f}"
            else:
                row["variance_description"] = vtype
    return rows


def _process_paid_in_full(rows: list[dict]) -> list[dict]:
    for row in rows:
        ip_paid = _to_number(row.get("ip_paid_amount") or row.get("ip_total_paid"))
        nip_paid = _to_number(row.get("nip_paid_amount") or row.get("nip_total_paid"))
        dtype = str(row.get("disputeType") or row.get("typeOfDispute") or "SINGLE").upper()
        era = _determine_pricing_era(row)
        pricing = _get_pricing(era)
        if dtype == "BATCHED":
            threshold = pricing.get("BATCHED", {}).get("base_total", 910.0)
        else:
            threshold = pricing.get(dtype, pricing.get("SINGLE", {})).get("total", 710.0)

        if ip_paid is not None:
            row["paid_in_full_ip"] = ip_paid >= threshold
            row["ip_payment_status"] = "Paid in full" if ip_paid >= threshold else f"${ip_paid:,.2f} of ${threshold:,.0f}"
        if nip_paid is not None:
            row["paid_in_full_nip"] = nip_paid >= threshold
            row["nip_payment_status"] = "Paid in full" if nip_paid >= threshold else f"${nip_paid:,.2f} of ${threshold:,.0f}"
        if ip_paid is not None and nip_paid is not None:
            row["both_paid_in_full"] = (ip_paid >= threshold) and (nip_paid >= threshold)
    return rows


def _process_historical_comparison(rows: list[dict]) -> list[dict]:
    if len(rows) < 2:
        return rows
    period_col = None
    for col in rows[0].keys():
        if re.search(r"\b(period|month|week|year|date|day)\b", col, re.IGNORECASE):
            period_col = col
            break
    if not period_col:
        return rows
    metric_cols = []
    for col in rows[0].keys():
        if col == period_col:
            continue
        vals = [_to_number(r.get(col)) for r in rows if r.get(col) is not None]
        if vals and all(v is not None for v in vals[:3]):
            metric_cols.append(col)
    if not metric_cols:
        return rows
    for i, row in enumerate(rows):
        if i == 0:
            for col in metric_cols:
                row[f"{col}_pct_change"] = None
        else:
            prev = rows[i - 1]
            for col in metric_cols:
                curr_val = _to_number(row.get(col))
                prev_val = _to_number(prev.get(col))
                if curr_val is not None and prev_val is not None and prev_val != 0:
                    pct = round(((curr_val - prev_val) / abs(prev_val)) * 100, 1)
                    row[f"{col}_pct_change"] = f"{'+' if pct >= 0 else ''}{pct}%"
                else:
                    row[f"{col}_pct_change"] = None
    return rows


def _collect_column_tooltips(rows: list[dict], original_cols: set) -> dict[str, str]:
    if not rows:
        return {}
    current_cols = set(rows[0].keys())
    new_cols = current_cols - original_cols
    tooltips = {}
    for col in new_cols:
        for pattern, doc in COMPUTED_COLUMN_DOCS.items():
            if col == pattern or col.endswith(pattern):
                tooltips[col] = doc
                break
        if col not in tooltips:
            tooltips[col] = f"Computed column added by post-processor"
    return tooltips


def _detect_needed_processors(query: str, sql: str, rows: list[dict]) -> list[str]:
    processors = []
    query_lower = (query or "").lower()
    sql_lower = (sql or "").lower()

    if rows and any("shortId" in r or "dispute_number" in r for r in rows[:5]):
        processors.append("dispute_numbers")
    if rows and any(k.endswith("Cents") or k.endswith("_cents")
                     for r in rows[:5] for k in r.keys()):
        processors.append("cents_to_dollars")
    if rows and any(k in r for r in rows[:5]
                     for k in ["due_date", "dueDate", "eligibilityDueDate",
                               "paymentDueDate", "due_date_until_decision"]):
        processors.append("urgency_scoring")
    if rows and any(("createdAt" in r or "created_at" in r) and
                     ("statusChangedAt" in r or "status_changed_at" in r or "closed_at" in r)
                     for r in rows[:5]):
        processors.append("processing_time")
    if rows and any("type" in r or "paymentType" in r or "direction" in r for r in rows[:5]):
        processors.append("payment_status")
    if "dispute_line_items" in sql_lower or "case_note" in sql_lower:
        processors.append("soft_delete_filter")
    if rows and any("disputeType" in r or "dispute_type" in r or "typeOfDispute" in r for r in rows[:5]):
        processors.append("expected_fees")
    if rows and any("varianceType" in r or "variance_type" in r or "varianceAmount" in r or "variance_amount" in r for r in rows[:5]):
        processors.append("payment_variance")
    if rows and any(
        any(k in r for k in ("ip_paid_amount", "nip_paid_amount", "ip_total_paid", "nip_total_paid",
                             "initiating_paid", "non_initiating_paid"))
        for r in rows[:5]
    ):
        processors.append("paid_in_full")
    if (len(rows) >= 2 and
        any(re.search(r"\b(period|month|week|year)\b", k, re.IGNORECASE)
            for k in rows[0].keys())):
        processors.append("historical_comparison")
    # Node 9 (2026-05-20): removed unconditional `user_transformations`
    # append — the loader read a file that has never existed.
    return processors


@trace_agent("v10.agent.post_processor")
def post_processor_node(state: GraphState) -> GraphState:
    rows = state.get("query_result")
    if not rows or not isinstance(rows, list) or len(rows) == 0:
        trace_entry = {
            "agent": "Post-Processor",
            "status": "ok",
            "summary": "No rows to process — skipped",
            "detail": [],
        }
        trace = state.get("agent_trace", []) + [trace_entry]
        return {**state, "agent_trace": trace}

    query = state.get("resolved_query") or state.get("user_query", "")
    sql = state.get("validated_sql") or state.get("generated_sql", "")
    original_cols = set(rows[0].keys()) if rows else set()

    needed = _detect_needed_processors(query, sql, rows)

    applied = []
    for proc in needed:
        if proc == "dispute_numbers":
            rows = _process_dispute_numbers(rows)
            applied.append("Dispute number formatting (DISP-XXXXXXX)")
        elif proc == "cents_to_dollars":
            rows = _process_cents_to_dollars(rows)
            applied.append("Cents-to-dollars conversion")
        elif proc == "urgency_scoring":
            rows = _process_urgency_scoring(rows)
            applied.append("Due date urgency scoring (EST-aware)")
        elif proc == "processing_time":
            rows = _process_processing_time(rows)
            applied.append("Processing time calculation (days)")
        elif proc == "payment_status":
            rows = _process_payment_status(rows)
            applied.append("Payment type/direction labels (from config)")
        elif proc == "soft_delete_filter":
            before = len(rows)
            rows = _process_soft_delete_filter(rows)
            after = len(rows)
            if before != after:
                applied.append(f"Soft-delete filter (removed {before - after} rows)")
            else:
                applied.append("Soft-delete filter (no rows removed)")
        elif proc == "expected_fees":
            rows = _process_expected_fees(rows)
            applied.append("Expected fee calculation (version-aware pricing)")
        elif proc == "payment_variance":
            rows = _process_payment_variance(rows)
            applied.append("Payment variance enrichment")
        elif proc == "paid_in_full":
            rows = _process_paid_in_full(rows)
            applied.append("Paid-in-full determination (era-aware threshold)")
        elif proc == "historical_comparison":
            rows = _process_historical_comparison(rows)
            applied.append("Period-over-period % change calculation")

    tooltips = _collect_column_tooltips(rows, original_cols)

    rules = _load_business_rules()
    rules_version = rules.get("version", "unknown")

    summary = f"Applied {len(applied)} post-processor(s)" if applied else "No post-processing needed"
    if tooltips:
        summary += f" · {len(tooltips)} computed column(s) documented"

    detail = applied if applied else ["All columns are raw SQL output — no enrichment needed"]
    if tooltips:
        detail.append(f"Column tooltips: {', '.join(sorted(tooltips.keys())[:8])}")
    if rules_version != "unknown":
        detail.append(f"Business rules version: {rules_version}")

    trace_entry = {
        "agent": "Post-Processor",
        "status": "ok",
        "summary": summary,
        "detail": detail,
    }
    trace = state.get("agent_trace", []) + [trace_entry]

    return {
        **state,
        "query_result": rows,
        "row_count": len(rows),
        "computed_column_tooltips": tooltips,
        "agent_trace": trace,
    }
