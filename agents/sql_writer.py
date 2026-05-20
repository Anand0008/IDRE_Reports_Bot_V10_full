"""
SQL Writer Agent — V10 (Node 4: Option E Hybrid)

Three-layer context delivery (per
local/docs/superpowers/reports/2026-05-20-node4-architecture-decision.md):

  L1 — CAG: SYSTEM_PROMPT (cached) carries always-on knowledge:
    HARD RULES, 8 CROSS-CUTTING RULES (named by ID), DISPLAY RULES,
    JOIN SKELETONS, MANDATORY PROTOCOL, OUTPUT FORMAT.

  L2 — Structured per-query retrieval: _build_user_message(state)
    prepends XML-wrapped blocks to the user message:
      <query>          user_query / resolved_query
      <schema>         state['schema_context'] (from schema_mapper +
                       schema_verifier, RBAC-filtered)
      <rules>          state['platform_context'] (from repurposed
                       platform_context_agent — matched cross-cutting
                       rules + report cards, including any
                       "Reference SQL:" line for known reports)
      <role>           user_role + permitted_tables
      <previous_attempt_failed>  on retry only

  L3 — Tools (MCP): mandatory verify_sql_executes EXPLAIN gate;
    drill-down tools (get_table_schema, get_enum_values,
    lookup_business_term, find_filter_pattern, get_idre_business_logic,
    list_available_reports) called only when L2 doesn't cover.

History:
  - V6: knowledge injection via metric_cards / sql_templates /
    successful_queries + ~100-line SYSTEM_PROMPT with {schema_context} +
    {platform_context} placeholders.
  - V8: replaced RAG with Gemini function-calling; 6 tools; dropped
    the {schema_context} + {platform_context} placeholders. SYSTEM_PROMPT
    cut to ~64 lines.
  - V10 (May 17): 8 tools; SYSTEM_PROMPT cut further to ~20 lines.
    Schema_mapper still built state['schema_context'] but nothing
    downstream read it.
  - V10 Node 4 (May 20): re-wired the seam. SYSTEM_PROMPT expanded
    to ~52 lines (8 rules + display + JOIN). Added _build_user_message
    to emit XML blocks from state. Tools kept as verification + drill-down.
"""
import json
import os
import re
import google.generativeai as genai
from google.generativeai.types.generation_types import StopCandidateException
from config.settings import get_settings
from state.context import GraphState
from tools.idre_tools import TOOL_DEFINITIONS, TOOL_DISPATCH
from tracing import trace_agent

# V10: metric_cards/sql_templates/successful_queries removed (replaced by tools)

# Single source of truth for the SQL-Writer tool-call budget. Referenced by
# SYSTEM_PROMPT (so the LLM is told the actual ceiling) and by
# _generate_sql_with_tools (so the loop enforces it). Increment with care:
# every round is one full Gemini turn plus N tool round-trips.
MAX_VERIFICATION_ROUNDS = 5

SYSTEM_PROMPT = f"""You are a MySQL expert writing SELECT queries for the IDRE healthcare dispute resolution platform.

The prompt you receive will include context blocks injected by the orchestrator:
  <query>      — the user's question (resolved if a clarification was applied)
  <schema>     — the table/column subset relevant to this question (omitted if empty)
  <rules>      — IDRE-specific rule snippets that apply to this question (omitted
                 if no rule matched); when the question matches a known IDRE report
                 this block also contains a "Reference SQL:" line you should use
                 as your starting point
  <role>       — the user's role and permitted_tables list (RBAC)
  <previous_attempt_failed> — present only on retry; carries the prior error
                              you must fix in the new SQL

HARD RULES
- SELECT-only. No INSERT/UPDATE/DELETE/DROP/ALTER/CREATE/TRUNCATE/EXEC/CALL.
- Always backtick `case` (MySQL reserved word). Backtick other reserved words too.
- Do NOT add LIMIT unless the user asks for a top-N.
- Use ONLY tables and columns listed in the <schema> block.

CROSS-CUTTING RULES (these apply globally; <rules> block surfaces the active ones with concrete snippets)
- R1 Outstanding payments: 5 statuses (PENDING_PAYMENTS, PENDING_SECOND_PAYMENT, PENDING_ADMINISTRATIVE_CLOSURE, INELIGIBLE, INELIGIBLE_PENDING_ADMIN_FEE) + NOT EXISTS subquery for "paid" check; party-specific (IP vs NIP).
- R2 Due dates: OR across 4 columns (due_date, due_date_until_decision, eligibilityDueDate, paymentDueDate). Use COALESCE(due_date, eligibilityDueDate, paymentDueDate, due_date_until_decision) for primary date.
- R3 CMS payments: payment.type = 'CASE_PAYMENT'. NEVER use 'CMS_INVOICE_PAYMENT' or 'CMS_ADMIN_FEE_TRANSFER' as filters for "CMS payment" questions.
- R4 Payouts to entities: match on LOWER(JSON_UNQUOTE(JSON_EXTRACT(p.bankingSnapshot, '$.accountHolderName'))) OR LOWER(p.recipientName). Always filter direction='OUTGOING' AND status='COMPLETED'.
- R5 Case balance: balance = SUM(allocated INCOMING APPROVED/COMPLETED) - SUM(refunds from case_refunds COMPLETED). Use case_refunds table for refunds, NOT case_payment_allocation. Do NOT subtract OUTGOING from INCOMING.
- R6 Recent activity: apply a 7-day filter when prompt says "recent/today/this week/latest". Use the :now placeholder; executor substitutes the temporal anchor.
- R7 Arbitrator/team queries: use case.assignedToId (NOT closed_by_user_id). Filter u.role IN ('arbitrator','arbitrator-contractor','admin-support','eligibility-specialist'). Use LEFT JOIN to include 0-case users.
- R8 Daily funds / net cash: SUM(payment.amount) GROUP BY direction, filtered to status IN ('APPROVED','COMPLETED'). NOT "count of new cases today".

DISPLAY RULES
- Always SELECT `case`.shortId AS dispute_number (NEVER expose `case`.id). The UI shows "DISP-<shortId>".
- When user searches by "DISP-XXXXXXX", filter WHERE `case`.shortId = 'XXXXXXX' (strip the DISP- prefix).
- For listings, ALWAYS include: `case`.shortId AS dispute_number, `case`.status, `case`.createdAt.
- Always filter dispute_line_items WHERE status = 'ACTIVE' (soft-delete; 'REMOVED' rows must not appear).
- Use human-readable column aliases (AS dispute_number, AS org_name, AS total_amount).
- case_refunds.refundAmountCents is in CENTS — divide by 100 for dollar display.

JOIN SKELETONS
- case ↔ user (arbitrator/assignee): JOIN `user` u ON `case`.assignedToId = u.id
- case ↔ payment (with allocation): JOIN case_payment_allocation cpa ON cpa.caseId = `case`.id; JOIN payment p ON p.id = cpa.paymentId
- case ↔ organization (NIP side): JOIN organization nip_org ON nip_org.id = `case`.nonInitiatingPartyOrganizationId
- case ↔ organization (IP side): JOIN organization ip_org ON ip_org.id = `case`.initiatingPartyOrganizationId
- case ↔ organization (owner): JOIN organization own_org ON own_org.id = `case`.ownedByOrganizationId
- case_party for party contact: JOIN case_party ON `case`.initiatingPartyId = case_party.id (for IP) OR `case`.nonInitiatingPartyId = case_party.id (for NIP). NOTE: case_party.partyType values are 'PROVIDER' or 'HEALTH_PLAN' — NOT 'INITIATING'/'NON_INITIATING'.

MANDATORY PROTOCOL
1. Consult the <schema> block for column names. Use ONLY tables and columns listed there. If you need a table that isn't in <schema>, call get_table_schema(table_name).
2. Consult the <rules> block for IDRE-specific conventions; apply each that fits.
3. If the <rules> block contains a "Reference SQL:" line (because the query matches a known IDRE report), use that SQL as your starting point.
4. For date phrases ("today", "month-to-date", "last 7 days") prefer the :now placeholder via find_filter_pattern.
5. Before declaring SQL final, call verify_sql_executes(sql). If it returns an error, fix and re-verify. Max {MAX_VERIFICATION_ROUNDS} verification rounds.

OUTPUT FORMAT
```sql
<final SQL>
```
ASSUMPTIONS:
- <one line per interpretive decision>
"""

_BREAKDOWN_WORDS = re.compile(
    r"\b(by|per|group|breakdown|split|each|list|show|which|who|detail|"
    r"organisation|organization|region|status|type|category|compare|"
    r"between|versus|vs|trend|over time|monthly|daily|weekly)\b", re.IGNORECASE)
_COUNT_INTENT = re.compile(
    r"^(how many|what is the (total|count|number)|count of|total number|"
    r"number of|how much|what('s| is) the)", re.IGNORECASE)


# V10: _check_metric_cards, _check_sql_templates, save_successful_query removed
# These were V6 patterns that competed with the new tool-driven flow.
# The LLM now uses get_idre_business_logic / verify_sql_executes instead.


# Node 6.1 (2026-05-20): SQL detection used to substring-match
# \b(SELECT|WITH|INSERT|UPDATE|DELETE)\b anywhere in the candidate text,
# which produced false positives on plain English (e.g. "users WITH roles",
# "status UPDATE", "I will SELECT recent"). The tiered parser below uses
# PREFIX-anchored checks instead — a SQL statement STARTS with SELECT or
# WITH after any leading comments, by language definition. INSERT/UPDATE/
# DELETE dropped since sql_writer is SELECT-only by HARD RULES.
_SQL_PREFIX_RE = re.compile(
    r"^(?:/\*.*?\*/\s*|--[^\n]*\n\s*)*(SELECT|WITH)\b",
    re.DOTALL | re.IGNORECASE,
)
# Tier 3: any LINE in the candidate starts with SELECT/WITH (after
# leading whitespace). Handles "Here's the query:\n\nSELECT * FROM ..."
# where Gemini prepended prose without a code fence.
_SQL_LINE_RE = re.compile(r"^\s*(SELECT|WITH)\b", re.IGNORECASE)


def _starts_with_sql(s: str) -> bool:
    """Tier 2: the candidate starts with SELECT or WITH after comments."""
    return bool(_SQL_PREFIX_RE.match(s.strip()))


def _extract_from_line_anchored(s: str) -> str:
    """Tier 3: find a line starting with SELECT/WITH; return everything
    from that line onward. Returns "" if no such line exists."""
    lines = s.splitlines()
    for idx, line in enumerate(lines):
        if _SQL_LINE_RE.match(line):
            return "\n".join(lines[idx:]).strip()
    return ""


def _parse_llm_response(raw: str) -> tuple[str, list[str]]:
    """Extract SQL + assumptions from Gemini output via tiered fallback.

    Tier 1 — fenced: a ```sql ... ``` (or ``` ... ```) code block whose
    content starts with SELECT/WITH wins. Everything outside is prose.

    Tier 2 — prefix-anchored: ASSUMPTIONS-split halves; whichever half
    starts with SELECT/WITH (after comments) is the SQL.

    Tier 3 — line-anchored: if neither half starts with SQL, scan each
    half for a LINE that starts with SELECT/WITH and treat from there.

    Tier 4 — give up: return empty SQL. The validator will reject with
    "Empty SQL generated." and the debugger's NO_SQL_EMITTED pattern
    surfaces a specific retry instruction back to the LLM.
    """
    sql_part = ""
    prose = raw

    # Tier 1: fenced block
    fence_match = re.search(
        r"```(?:sql)?\s*\n(.*?)(?:```|$)",
        raw, re.DOTALL | re.IGNORECASE,
    )
    if fence_match and _starts_with_sql(fence_match.group(1)):
        sql_part = fence_match.group(1).strip()
        prose = (raw[:fence_match.start()] + raw[fence_match.end():]).strip()
    else:
        # No (valid) fence — try ASSUMPTIONS-split, then line-anchored
        match = re.search(r"\bASSUMPTIONS\s*:\s*\n?", raw, re.IGNORECASE)
        if match:
            before = raw[: match.start()].strip()
            after = raw[match.end():].strip()
        else:
            before = raw.strip()
            after = ""

        # Tier 2: prefix-anchored on either half (after gets preference —
        # Gemini's standard format is ASSUMPTIONS first, SQL second).
        if _starts_with_sql(after):
            sql_part, prose = after, before
        elif _starts_with_sql(before):
            sql_part, prose = before, after
        else:
            # Tier 3: scan each half for an inline SELECT/WITH line.
            after_extract = _extract_from_line_anchored(after)
            before_extract = _extract_from_line_anchored(before)
            if after_extract:
                sql_part, prose = after_extract, before
            elif before_extract:
                sql_part, prose = before_extract, after
            else:
                # Tier 4: no SQL anywhere — empty + full raw as prose
                sql_part = ""
                prose = raw

        # Strip any trailing fence markers that escaped tier 1
        if sql_part:
            sql_part = re.sub(r"\s*```\s*$", "", sql_part).strip()
            sql_part = re.sub(r"^```(?:sql)?\s*", "", sql_part, flags=re.IGNORECASE).strip()

    # Extract assumptions bullets from prose (unchanged from V10)
    assumptions = []
    in_assumptions = False
    for line in prose.splitlines():
        if re.search(r"\bASSUMPTIONS\s*:?\s*$", line, re.IGNORECASE):
            in_assumptions = True
            continue
        cleaned = line.strip().lstrip("-").lstrip("*").strip()
        if not cleaned or cleaned.startswith("```"):
            continue
        if in_assumptions or cleaned.startswith(("-", "*")) or "assume" in cleaned.lower():
            assumptions.append(cleaned)
    # Fallback: no ASSUMPTIONS marker → take all non-empty bullet-ish lines
    if not assumptions and prose:
        for line in prose.splitlines():
            cleaned = line.strip().lstrip("-").lstrip("*").strip()
            if cleaned and not cleaned.startswith("```"):
                assumptions.append(cleaned)
    return sql_part, assumptions


def _build_user_message(state: dict, error_context: str = "") -> str:
    """Compose the per-query user message with XML-wrapped context blocks.

    Layer 2 of Option E. Blocks emitted only when their state field is
    non-empty so we don't waste tokens on empty <schema></schema> tags.
    Layer 1 (SYSTEM_PROMPT, cached) carries the always-on rules.
    """
    query = state.get("resolved_query") or state.get("user_query", "")
    schema_context = (state.get("schema_context") or "").strip()
    platform_context = (state.get("platform_context") or "").strip()
    permitted = state.get("permitted_tables") or []
    role = state.get("user_role") or ""

    parts: list[str] = [f"<query>\n{query}\n</query>"]

    if schema_context:
        parts.append(f"<schema>\n{schema_context}\n</schema>")

    if platform_context:
        parts.append(f"<rules>\n{platform_context}\n</rules>")

    if role or permitted:
        permitted_str = (
            f"permitted_tables ({len(permitted)}): {', '.join(permitted)}"
            if permitted else "permitted_tables: (no role restriction)"
        )
        parts.append(f"<role>\nRole {role} — {permitted_str}\n</role>")

    if error_context:
        parts.append(f"<previous_attempt_failed>\n{error_context[:600]}\n</previous_attempt_failed>")

    return "\n\n".join(parts)


def _build_gemini_tools() -> list[dict]:
    """Convert our tool definitions to Gemini function declaration format."""
    tools = []
    for defn in TOOL_DEFINITIONS:
        tools.append(genai.protos.Tool(
            function_declarations=[
                genai.protos.FunctionDeclaration(
                    name=defn["name"],
                    description=defn["description"],
                    parameters=genai.protos.Schema(
                        type=genai.protos.Type.OBJECT,
                        properties={
                            k: genai.protos.Schema(
                                type=genai.protos.Type.STRING,
                                description=v.get("description", ""),
                            )
                            for k, v in defn["parameters"].get("properties", {}).items()
                        },
                        required=defn["parameters"].get("required", []),
                    ),
                )
            ]
        ))
    return tools


def _generate_sql_with_tools(
    state: dict, error_context: str = "", max_tool_rounds: int = MAX_VERIFICATION_ROUNDS
) -> tuple[str, list[str], dict, list[dict]]:
    """Generate SQL using Gemini function calling with MCP tools.

    The user message is built via `_build_user_message(state, error_context)`
    which prepends <query>/<schema>/<rules>/<role> XML blocks (Layer 2).
    The cached SYSTEM_PROMPT carries always-on rules (Layer 1).
    Returns (sql, assumptions, token_usage, tool_calls_log).
    """
    settings = get_settings()
    genai.configure(api_key=settings.gemini_api_key)

    model = genai.GenerativeModel(
        model_name="gemini-3.1-pro-preview",
        system_instruction=SYSTEM_PROMPT,
        tools=_build_gemini_tools(),
    )

    user_message = _build_user_message(state, error_context=error_context)

    chat = model.start_chat()

    tool_calls_log: list[dict] = []
    total_tokens = {"input": 0, "output": 0, "total": 0}
    rounds = 0

    # Node 6.1 (2026-05-20): Gemini's SDK can raise StopCandidateException
    # with finish_reason: MALFORMED_FUNCTION_CALL when its function-call
    # JSON is unparseable. Previously this propagated out and crashed the
    # entire orchestrator. Now we catch it, log a marker, and return empty
    # SQL — the validator then rejects with "Empty SQL generated." and the
    # debugger's NO_SQL_EMITTED pattern gives a useful retry instruction.
    try:
        response = chat.send_message(user_message)
    except (StopCandidateException, Exception) as exc:
        tool_calls_log.append({
            "tool": "_gemini_error_marker",
            "stage": "initial_send",
            "error": str(exc)[:200],
            "result_length": 0,
        })
        return "", [], total_tokens, tool_calls_log

    while rounds < max_tool_rounds:
        if not response.candidates:
            break

        candidate = response.candidates[0]

        if not candidate.content.parts:
            break

        has_function_call = False
        function_responses = []

        for part in candidate.content.parts:
            if hasattr(part, 'function_call') and part.function_call.name:
                has_function_call = True
                fn_name = part.function_call.name
                fn_args = dict(part.function_call.args) if part.function_call.args else {}

                tool_fn = TOOL_DISPATCH.get(fn_name)
                if tool_fn:
                    from tracing import traced_tool_call
                    with traced_tool_call(fn_name) as _tool_span:
                        if _tool_span is not None:
                            try:
                                _tool_span.set_attribute("tool.args_keys", list(fn_args.keys())[:20])
                            except Exception:
                                pass
                        result = tool_fn(**fn_args)
                        if _tool_span is not None:
                            try:
                                _tool_span.set_attribute("tool.result_size", len(str(result)[:10000]))
                            except Exception:
                                pass
                else:
                    result = f"Unknown tool: {fn_name}"

                tool_calls_log.append({
                    "tool": fn_name,
                    "args": fn_args,
                    "result_length": len(result),
                })

                function_responses.append(
                    genai.protos.Part(
                        function_response=genai.protos.FunctionResponse(
                            name=fn_name,
                            response={"result": result},
                        )
                    )
                )

        if not has_function_call:
            break

        try:
            response = chat.send_message(function_responses)
        except (StopCandidateException, Exception) as exc:
            tool_calls_log.append({
                "tool": "_gemini_error_marker",
                "stage": "tool_response_send",
                "error": str(exc)[:200],
                "result_length": 0,
            })
            # Bail out of the loop; downstream code reads `response` —
            # leave whatever the prior turn produced so the final-text
            # extraction below still runs (typically yields "").
            break
        rounds += 1

    if hasattr(response, 'usage_metadata'):
        usage = response.usage_metadata
        total_tokens = {
            "input": getattr(usage, 'prompt_token_count', 0),
            "output": getattr(usage, 'candidates_token_count', 0),
            "total": getattr(usage, 'total_token_count', 0),
        }

    final_text = ""
    if response.candidates:
        for part in response.candidates[0].content.parts:
            if hasattr(part, 'text') and part.text:
                final_text += part.text

    raw = final_text.strip()
    raw = re.sub(r"^```(?:sql)?\s*", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\s*```$", "", raw)
    sql, assumptions = _parse_llm_response(raw)

    return sql, assumptions, total_tokens, tool_calls_log


def _check_explain_plan(sql: str) -> dict:
    """Post-emit EXPLAIN check for performance warnings surfaced into agent_trace.

    Intentionally separate from `tools.idre_tools.verify_sql_executes`:
      - `verify_sql_executes` is the LLM-callable correctness gate during
        composition — it returns ``ok: bool`` + error message back into the
        Gemini conversation so the model can self-correct.
      - `_check_explain_plan` runs AFTER the LLM emits its final SQL and
        produces the ``full_scan_tables`` warning consumed by the framework
        (agent_trace summary, observability spans). The LLM does NOT see
        this result.

    Two different consumers, two EXPLAIN round-trips. EXPLAIN is cheap on
    indexed queries (<5 ms typical); merging would couple the LLM tool-result
    shape to the framework's trace shape and was rejected in Node 6 (D4.b).
    See: local/docs/superpowers/reports/2026-05-20-node6-architecture-decision.md
    """
    try:
        from db.connector import get_engine
        from sqlalchemy import text as sa_text
        engine = get_engine()
        with engine.connect() as conn:
            result = conn.execute(sa_text(f"EXPLAIN {sql[:2000]}"))
            rows = result.fetchall()
            total_rows_scanned = 0
            full_scan_tables = []
            for row in rows:
                row_count = row[8] if len(row) > 8 and row[8] else 0
                try:
                    row_count = int(row_count)
                except (TypeError, ValueError):
                    row_count = 0
                total_rows_scanned += row_count
                access_type = row[3] if len(row) > 3 else ""
                table_name = row[2] if len(row) > 2 else ""
                if access_type == "ALL" and row_count > 100000:
                    full_scan_tables.append(f"{table_name} ({row_count:,} rows)")

            return {
                "total_rows": total_rows_scanned,
                "full_scan_tables": full_scan_tables,
                "warning": bool(full_scan_tables),
            }
    except Exception:
        return {"total_rows": 0, "full_scan_tables": [], "warning": False}


@trace_agent("v10.agent.sql_writer")
def sql_writer_node(state: GraphState) -> GraphState:
    error_context = state.get("retry_context", "") or state.get("execution_error", "") or ""
    retry_count = state.get("retry_count", 0)

    # Option E Layer 2: build XML-wrapped user message from state.
    sql, assumptions, tok, tool_calls = _generate_sql_with_tools(state, error_context)

    token_usage = dict(state.get("token_usage") or {})
    writer_key = "SQL Writer" if retry_count == 0 else f"SQL Writer (retry {retry_count})"
    token_usage[writer_key] = tok

    explain = _check_explain_plan(sql)

    label = "Retry" if retry_count > 0 else "Attempt 1"
    detail = []
    if error_context:
        detail.append(f"Previous error: {error_context[:120]}")
    if tool_calls:
        tool_names = [tc["tool"] for tc in tool_calls]
        detail.append(f"Tools called: {', '.join(tool_names)}")
    if assumptions:
        detail.append(f"{len(assumptions)} assumption(s) annotated")
    if explain.get("warning"):
        detail.append(f"Full table scan: {', '.join(explain['full_scan_tables'])}")

    trace_entry = {
        "agent": "SQL Writer",
        "status": "ok",
        "summary": f"SQL generated via Gemini + {len(tool_calls)} tool call(s) · {label}"
        + (f" · {len(assumptions)} assumption(s)" if assumptions else ""),
        "detail": detail,
    }
    trace = state.get("agent_trace", []) + [trace_entry]

    return {
        **state,
        "generated_sql": sql,
        "assumptions": assumptions,
        "agent_trace": trace,
        "execution_error": None,
        "token_usage": token_usage,
        "explain_plan": explain,
        # 2026-05-21: cumulative across retries so the audit + feedback
        # writers capture every tool call across every attempt.
        "tool_calls_log": (state.get("tool_calls_log") or []) + tool_calls,
    }
