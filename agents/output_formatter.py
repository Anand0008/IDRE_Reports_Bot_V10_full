"""
Output Formatter Agent — V10

Cell-level value formatting. For each column in the result set,
detects the value type (currency / date / days / count / percentage /
raw) from the column name + sample values, then formats each cell
consistently. Currency formatting is locale-aware (US/EU/UK via
user_preferences.locale) for separators + date format — the currency
PREFIX is always `$` since IDRE settles in USD only. Percentages
auto-scale (0-1 vs 0-100) based on observed sample range. Emits
cell_styles metadata for conditional formatting (red for negative
amounts, urgency-color for due_date buckets, green/red for paid_in_full
booleans, signed pct_change).

NOT to be confused with response_formatter — that one decides chart
type and layout; this one shapes values inside cells.

History:
- V3/V4: agent introduced — base cell formatters (currency, date,
  count, raw) keyed off column-name tokens.
- V5+: minor byte-level deltas (~410 bytes) over V3/V4; same shape.
- V6: user format preferences (per-session overrides for date/currency
  format), conditional formatting (red/green/bold cell metadata),
  unit-aware percentage detection, locale support (US/EU/UK), days
  detection (_DAYS_KEYWORDS).
- V7/V8/V9: byte-identical to V6 — four versions of architectural
  stability.
- V10 (May 17): @trace_agent("v10.agent.output_formatter") decorator
  + tracing import; no behavioural change vs V9.
- V10 Node 10 (May 20): module header refreshed; added
  _TIME_WINDOW_SUFFIX_RE to fix smoke-test S3 — columns named
  `*_(last|in|within|past|previous)_N_(day|days|...)$` were being
  mis-classified as `days` because the trailing time-window qualifier
  matched the `_DAYS_KEYWORDS` set. The 1020-cases COUNT result was
  being rendered as "1,020 days". The regex now intercepts the suffix
  pattern before the days-keyword check, so the value is treated as a
  count. See
  local/docs/superpowers/reports/2026-05-20-node10-formatters-audit.md.
"""
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from state.context import GraphState
from tracing import trace_agent


_CAMEL_SPLIT = re.compile(r"(?<!^)(?=[A-Z])")


def _tokenize(col_name: str) -> set:
    snake = _CAMEL_SPLIT.sub("_", col_name).lower()
    return {t for t in re.split(r"[_\-\s]+", snake) if t}


_CURRENCY_KEYWORDS = {
    "amount", "balance", "payment", "fee", "revenue", "cost", "price", "value",
    "disbursement", "refund", "charge", "paid", "owed", "earned",
    "dollar", "dollars", "usd", "money", "total", "subtotal", "grandtotal",
    "variance", "allocated", "expected",
}
_AMBIG_CURRENCY = {"total", "value", "expected"}

_DATE_KEYWORDS = {
    "date", "time", "created", "updated", "closed", "opened", "filed",
    "submitted", "received", "rendered", "changed", "month", "year",
    "period", "timestamp", "due", "scheduled", "paidat",
}
_DATE_SUFFIX_RE = re.compile(r"(?:_at|_on|At|On)$")
_DAYS_KEYWORDS = {"days", "duration", "elapsed", "turnaround", "lag"}
_PERCENTAGE_KEYWORDS = {"percent", "pct", "rate", "ratio", "proportion", "percentage", "win"}
_COUNT_KEYWORDS = {"count", "num", "qty", "quantity", "cnt", "total"}

# Node 10 (2026-05-20, smoke S3 fix): a column whose name ends with a
# time-window qualifier like `_last_7_days` / `_in_30_days` /
# `_within_60_days` / `_past_2_weeks` / `_previous_3_months` is reporting
# a COUNT (or SUM) OVER that window, not a duration. The previous
# `_DAYS_KEYWORDS` check treated any column containing the token "days"
# as a duration, so the COUNT result of `cases_created_last_7_days = 1020`
# was being rendered as "1,020 days". This regex intercepts the suffix
# pattern in `_detect_col_type` before the days-keyword check fires.
_TIME_WINDOW_SUFFIX_RE = re.compile(
    r"_(last|in|within|past|previous)_\d+_(day|days|week|weeks|month|months|year|years)$",
    re.IGNORECASE,
)


# Locale configs differ only in date format + decimal/thousands separators.
# The `currency_prefix` is always `$` because IDRE settles in USD globally —
# the locale switch is for separator semantics + date layout, not currency.
_LOCALE_CONFIGS = {
    "US": {
        "date_format": "%b {day}, %Y",
        "date_format_with_time": "%b {day}, %Y %H:%M",
        "decimal_sep": ".",
        "thousands_sep": ",",
        "currency_prefix": "$",
    },
    "EU": {
        "date_format": "{day}. %b %Y",
        "date_format_with_time": "{day}. %b %Y %H:%M",
        "decimal_sep": ",",
        "thousands_sep": ".",
        "currency_prefix": "$",
    },
    "UK": {
        "date_format": "{day} %b %Y",
        "date_format_with_time": "{day} %b %Y %H:%M",
        "decimal_sep": ".",
        "thousands_sep": ",",
        "currency_prefix": "$",
    },
}


def _get_locale_config(preferences: dict) -> dict:
    locale = (preferences or {}).get("locale", "US")
    return _LOCALE_CONFIGS.get(locale, _LOCALE_CONFIGS["US"])


def _fmt_currency(val, preferences: dict = None) -> str:
    locale = _get_locale_config(preferences)
    no_cents = (preferences or {}).get("currency_no_cents", False)
    try:
        if isinstance(val, Decimal):
            f = float(val)
        elif isinstance(val, str):
            f = float(Decimal(val))
        else:
            f = float(val)
        # Format the absolute value, then attach sign + currency prefix
        # in standard accounting order: `-$50.00`, not `$-50.00`.
        abs_formatted = f"{abs(f):,.0f}" if no_cents else f"{abs(f):,.2f}"
        if locale["decimal_sep"] != ".":
            abs_formatted = (
                abs_formatted.replace(",", "TEMP")
                .replace(".", locale["decimal_sep"])
                .replace("TEMP", locale["thousands_sep"])
            )
        sign = "-" if f < 0 else ""
        return f"{sign}{locale['currency_prefix']}{abs_formatted}"
    except (TypeError, ValueError, InvalidOperation):
        return str(val)


def _fmt_date(val, preferences: dict = None) -> str:
    locale = _get_locale_config(preferences)
    custom_format = (preferences or {}).get("date_format")

    def _clean(dt: datetime) -> str:
        if custom_format:
            try:
                return dt.strftime(custom_format)
            except ValueError:
                pass
        day = dt.strftime("%d").lstrip("0")
        if dt.hour or dt.minute:
            return dt.strftime(locale["date_format_with_time"].replace("{day}", day))
        return dt.strftime(locale["date_format"].replace("{day}", day))

    if isinstance(val, datetime):
        return _clean(val)
    if isinstance(val, date):
        day = val.strftime("%d").lstrip("0")
        if custom_format:
            try:
                return val.strftime(custom_format)
            except ValueError:
                pass
        return val.strftime(locale["date_format"].replace("{day}", day))
    if isinstance(val, str):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d", "%Y-%m"):
            try:
                dt = datetime.strptime(val.strip(), fmt)
                if fmt == "%Y-%m":
                    return dt.strftime("%b %Y")
                return _clean(dt)
            except ValueError:
                continue
    return str(val)


def _fmt_days(val) -> str:
    try:
        n = int(float(val))
        return f"{n:,} days"
    except (TypeError, ValueError):
        return str(val)


def _fmt_count(val) -> str:
    try:
        n = int(float(val))
        return f"{n:,}"
    except (TypeError, ValueError):
        return str(val)


def _fmt_percentage(val, col_name: str = "", sample_vals: list = None) -> str:
    try:
        f = float(val)
        if sample_vals:
            non_null = [float(v) for v in sample_vals if v is not None]
            if non_null and all(0 <= v <= 1.0 for v in non_null[:5]):
                f = f * 100
        elif 0 < f < 1.0 and not str(val).endswith("%"):
            f = f * 100
        return f"{f:.2f}%"
    except (TypeError, ValueError):
        return str(val)


def _first_non_null(sample_vals: list):
    return next((v for v in sample_vals if v is not None), None)


def _name_says_currency(tokens: set, col_name: str) -> bool:
    hits = tokens & _CURRENCY_KEYWORDS
    if not hits:
        return False
    if hits <= _AMBIG_CURRENCY:
        return False
    return True


def _name_says_date(tokens: set, col_name: str) -> bool:
    if tokens & _DATE_KEYWORDS:
        return True
    return bool(_DATE_SUFFIX_RE.search(col_name))


def _detect_col_type(col_name: str, sample_vals: list) -> str:
    tokens = _tokenize(col_name)
    first_val = _first_non_null(sample_vals)

    # Node 10 (smoke S3 fix): a `_last_N_days` / `_in_N_days` / etc.
    # trailing qualifier denotes a time WINDOW for a count, not the unit
    # of the value. Intercept before any days-keyword detection fires.
    is_time_window_count = bool(_TIME_WINDOW_SUFFIX_RE.search(col_name))

    if first_val is not None:
        if isinstance(first_val, (date, datetime)):
            return "date"
        if isinstance(first_val, bool):
            return "raw"
        if isinstance(first_val, int):
            if is_time_window_count:
                return "count"
            if tokens & _DAYS_KEYWORDS:
                return "days"
            return "count"
        if isinstance(first_val, str):
            if _name_says_date(tokens, col_name):
                return "date"
            return "raw"

    numeric = isinstance(first_val, (Decimal, float))
    if isinstance(first_val, Decimal):
        _, _, exponent = first_val.as_tuple()
        if isinstance(exponent, int) and exponent < 0:
            if is_time_window_count:
                return "currency"  # still currency, not days — narrow safety
            if tokens & _DAYS_KEYWORDS:
                return "days"
            return "currency"

    if tokens & _PERCENTAGE_KEYWORDS:
        return "percentage"
    if _name_says_currency(tokens, col_name):
        return "currency"
    if _name_says_date(tokens, col_name):
        return "date"
    if not is_time_window_count and tokens & _DAYS_KEYWORDS:
        return "days"
    if numeric and tokens & _AMBIG_CURRENCY:
        return "currency"
    if tokens & _COUNT_KEYWORDS or is_time_window_count:
        return "count"

    return "raw"


def _compute_conditional_format(col_name: str, col_type: str, val, formatted_val) -> dict:
    style = {}
    if val is None:
        return style
    tokens = _tokenize(col_name)
    if col_type == "currency":
        try:
            num = float(val) if not isinstance(val, (int, float)) else val
            if num < 0:
                style["color"] = "#E74C3C"
        except (TypeError, ValueError):
            pass
    if "urgency" in col_name.lower():
        val_str = str(val).lower()
        if val_str == "overdue":
            style["color"] = "#E74C3C"
            style["font-weight"] = "bold"
        elif val_str == "urgent":
            style["color"] = "#E67E22"
            style["font-weight"] = "bold"
        elif val_str == "warning":
            style["color"] = "#F39C12"
        elif val_str == "normal":
            style["color"] = "#27AE60"
    if "paid_in_full" in col_name.lower():
        if val is True:
            style["color"] = "#27AE60"
            style["font-weight"] = "bold"
        elif val is False:
            style["color"] = "#E74C3C"
    if col_name.endswith("_pct_change") and isinstance(formatted_val, str):
        if formatted_val.startswith("+"):
            style["color"] = "#27AE60"
        elif formatted_val.startswith("-"):
            style["color"] = "#E74C3C"
    return style


def _format_rows(rows: list, preferences: dict = None) -> tuple[list, dict, dict]:
    if not rows:
        return rows, {}, {}

    cols = list(rows[0].keys())
    col_types = {}
    for col in cols:
        sample = [r[col] for r in rows[:10] if r.get(col) is not None]
        col_types[col] = _detect_col_type(col, sample)

    cell_styles = {}
    formatted = []
    for row_idx, row in enumerate(rows):
        new_row = {}
        for col in cols:
            val = row.get(col)
            if val is None:
                new_row[col] = None
                continue
            ctype = col_types[col]
            if ctype == "percentage":
                sample = [r[col] for r in rows[:10] if r.get(col) is not None]
                new_row[col] = _fmt_percentage(val, col, sample)
            elif ctype == "currency":
                new_row[col] = _fmt_currency(val, preferences)
            elif ctype == "date":
                new_row[col] = _fmt_date(val, preferences)
            elif ctype == "days":
                new_row[col] = _fmt_days(val)
            elif ctype == "count":
                new_row[col] = _fmt_count(val)
            else:
                new_row[col] = val
            cond_style = _compute_conditional_format(col, ctype, val, new_row[col])
            if cond_style:
                cell_styles[f"{row_idx}:{col}"] = cond_style
        formatted.append(new_row)

    return formatted, col_types, cell_styles


@trace_agent("v10.agent.output_formatter")
def output_formatter_node(state: GraphState) -> GraphState:
    rows = state.get("query_result")
    if not rows:
        return state

    preferences = state.get("user_preferences") or {}
    formatted, col_types_summary, cell_styles = _format_rows(rows, preferences)

    applied = [f"{col}->{t}" for col, t in col_types_summary.items() if t != "raw"]

    detail = applied if applied else []
    if cell_styles:
        detail.append(f"Conditional formatting: {len(cell_styles)} styled cell(s)")
    locale = preferences.get("locale", "US")
    if locale != "US":
        detail.append(f"Locale: {locale}")

    trace_entry = {
        "agent": "Output Formatter",
        "status": "ok",
        "summary": (
            f"Formatted {len(applied)} column(s)" if applied
            else "No formatting needed — all columns are plain values"
        ) + (f" · {len(cell_styles)} conditional style(s)" if cell_styles else ""),
        "detail": detail,
    }

    return {
        **state,
        "query_result": formatted,
        "cell_styles": cell_styles,
        "agent_trace": state.get("agent_trace", []) + [trace_entry],
    }
