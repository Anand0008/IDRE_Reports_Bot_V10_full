# IDRE Reports Bot — V10 Demo Guide

## Quick Start

```bash
cd <HOME>\Downloads\v10_reports_bot
source venv/Scripts/activate   # Windows Git Bash
streamlit run app.py
```

Visit **http://localhost:8501** in your browser. You'll need:
1. The app password (set in `.env` as `APP_PASSWORD`).
2. A user handle (any name, used to tag feedback/audit records).
3. A persona selected from the sidebar (default: VO View-Only).

## What's running under V10

### Two execution paths

```
User: "show me cases due today"
      ↓
router.route() → confidence 0.87 → path=known
      ↓
IdreApiClient.call("due-dates", {urgency:"all", limit:10000})
      ↓
Response: IDRE's actual /api/reports/due-dates JSON
```

```
User: "average payment amount per dispute type this month"
      ↓
router.route() → score 0.0 → path=derived
      ↓
14-agent LangGraph pipeline:
   context_loader → ambiguity_scorer → clarification_agent →
   schema_mapper ∥ platform_context → schema_verifier →
   sql_writer (Gemini 2.5 Pro + 8 tools) → sql_validator →
   executor → post_processor → output_formatter → response_formatter
```

### Live infrastructure

| Service | Endpoint | State |
|---|---|---|
| IDRE local (Next.js prod build) | http://127.0.0.1:3000 | Reads local docker `idre` (staging snapshot — 67,794 cases) |
| Local docker MySQL | container `idre-mysql:3306`, DB `idre` | Backed by `.snapshots/staging_snapshot_*.sql.gz` |
| Jaeger (OTel traces) | http://localhost:16686 | Service `v10-bot`; restart container to clear |
| Streamlit V10 UI | http://localhost:8501 | This bot |

## Demo Script

### 1. Known-path query (~3-5s)

**Type:** `"show me the dashboard overview"`

**Flow:**
- Router matches multi-trigger ("dashboard" + "overview") → score 0.90 → known
- IDRE API call → `/api/reports/dashboard-stats` → `{totalCases, totalPayments, avgProcessingTime, activeArbitrators}`
- Response normalizer flattens shape; renders as a 4-card KPI grid

**Trace check:** Open Jaeger UI → service `v10-bot` → most recent trace shows `v10.query > v10.router.route > v10.known.api_call`.

### 2. Derived-path query, count-intent (~10-20s)

**Type:** `"how many cases are pending RFI"`

**Flow:**
- Router score < 0.85 (count-intent demote on single-word triggers) → derived
- context_loader: no anaphora, glossary hit on "pending RFI"
- ambiguity_scorer: 0.0% (term resolved by glossary)
- schema_mapper picks `case` (intent regex: payment_pattern → no match; org_pattern → no match; default to BM25 top-3 on "case" terms)
- sql_writer calls `lookup_business_term("pending RFI")` → returns SQL filter `status IN ('PENDING_RFI','PENDING_INITIAL_RFI')`; writes `SELECT COUNT(*) FROM case WHERE status IN (...)`
- sql_validator: column existence OK, role allows `case`
- executor: 382 (current snapshot)
- response_formatter: format=number

**Trace check:** Same query in Jaeger should show full `v10.derived.orchestrator > v10.agent.*` hierarchy, including `v10.tool.lookup_business_term`.

### 3. Anaphora resolution (~10-20s)

**Type:** `"show me the dashboard overview"` then `"what about for batched cases?"`

**Flow:**
- Turn 2: context_loader detects `"what about"` (multi-word anaphora trigger) → uses LLM resolver
- Resolved query: `"show me the dashboard overview for batched cases"`
- Router still matches dashboard-stats; bot calls IDRE with `caseTypes=BATCHED`

**If you want to see the resolved_query in the response:** sidebar → Developer Options → "Show agent trace".

### 4. Feedback retry (Flow B)

**After any answer:** sidebar → "Query Feedback Mode" → ✗ No → check 1-2 error categories → "Retry with Correction"

**Flow:**
- App writes the original feedback record to `data/feedback_log.jsonl`
- Re-runs the pipeline with `is_feedback_retry=True` and `feedback_correction_context={original_query, error_categories, free_text_note, ...}`
- Pipeline bypasses query_cache; `feedback_injector_node` prepends a correction block to `retry_context`
- SQL Writer sees both the corrected hint and the original prompt; generates fresh SQL

### 5. Saved queries

**Type:** `"save this as my_daily_status"` after any successful answer. Run later via the sidebar "▶ Run" button. Backed by `data/saved_queries.json`.

### 6. Developer toggles

Sidebar → Developer Options enables:
- **Show SQL** — surface the validated SQL in the response
- **Show agent trace** — full pipeline timeline with per-agent icons + status badges
- **Show assumptions** — interpretive decisions the writer flagged
- **Query explanation** — plain-English description of the SQL
- **Proactive suggestions** — 3 follow-up question chips
- **Token usage** — per-LLM-call input/output/total tokens
- **Usage stats panel** — today's query count, success rate, top tables, recent errors
- **Query Feedback Mode** — inline Yes/No + retry-with-correction

## Persona table (V10)

| Code | Role | What they see |
|---|---|---|
| MA | Master Admin | Everything (~30 tables, all reports) |
| PA | Payment Admin | Full financial — payments, invoices, NACHA |
| PS | Payment Specialist | Payment processing, refunds |
| AC | Accounting | Case balance, daily funds, reconciliation |
| AM | Arbitrator Manager | Team performance, case assignments |
| CB | Capitol Bridge Admin | Outstanding payments, payouts |
| VT | VeraTru Support | Limited read-only |
| VO | View Only (default) | No financial detail |
| DQD | Data Quality Debugger | Full reporting tables (no auth tables) |

## Troubleshooting

### Streamlit won't start
```bash
pkill -f streamlit
source venv/Scripts/activate
streamlit run app.py
```

### Database connection timeout
- Check `.env` is set: `DB_HOST` / `DB_USER` / `DB_PASSWORD` / `DB_SSL_CA`
- Test connection: `py311 -c "from db.connector import get_engine; get_engine().connect(); print('OK')"`

### Gemini API key invalid
- Verify `.env`: `Gemini_API_Key=<redacted>`
- Check Gemini console for rate limits (100 RPM default tier)

### Jaeger not capturing traces
- Confirm container is running: `docker ps | grep v10-jaeger`
- Restart if needed: `bash ../local/scripts/snapshot/start_jaeger.sh`
- Confirm `V10_OTEL_ENABLED` is unset or `1`

### Auto-login fails
- IDRE local must be running on port 3000
- `/api/dev/auto-login` is dev-only — `NODE_ENV=development` must be set even if running `next start` (see `local/docs/idre-local-prod-mode.md`)
- Ryan's password row must be `dbfd6621...:d667b791...` (the IDRE hardcoded seed hash for `orchid123`); twoFactor row for Ryan must be deleted

## Key files to know

| File | Purpose |
|---|---|
| `app.py` | Streamlit UI + chat loop + feedback panel |
| `harness_entrypoint.py` | Single-call API for pytest / CLI / orchestration |
| `core/orchestrator.py` | LangGraph pipeline definition |
| `agents/` | 17 .py — one node per agent + adapters for the known path |
| `tools/idre_tools.py` | The 8 in-process tools the SQL Writer calls |
| `state/context.py` | GraphState TypedDict (every field that flows through the pipeline) |
| `tracing.py` | OTEL helper — `@trace_agent`, `traced_tool_call`, `redact()` |
| `config/route_signatures.json` | 12 known-report signatures (router lookup) |
| `config/business_glossary.json` | Domain term → SQL filter mappings |
| `knowledge/v10/manifest.json` | SHA + validation state of the active knowledge artifacts |
| `data/audit_log.jsonl` | Per-query audit trail (async, append-only) |
| `data/feedback_log.jsonl` | Per-response feedback records (Flow B) |

## History

- **V1–V5** (2025–early 2026): shipped versions; small per-table agents; metric-card-driven SQL fast paths; RAG with ChromaDB; weighted scoring on test passes.
- **V6** (April 2026): added entity registry, debugger LLM classifier, RBAC, audit trail, business glossary.
- **V7** (April 2026, evaluated only): RAG measurably hurt accuracy (-20.6% vs V8); retired.
- **V8** (April 2026, evaluated only): removed RAG + embeddings; MCP-style 6 tools; 65-line system prompt.
- **V9** (April 2026, evaluated only): runtime verifier; broken (compared with `limit=25` against bot results up to 200K rows).
- **V10** (May 2026, current): 12 hardcoded known-report signatures + 8 in-process tools + Gemini 2.5 Pro writer; knowledge regenerated at build time; OTel/Jaeger traces; result-comparison test harness.

For the full design contract: `local/docs/superpowers/specs/2026-05-15-v10-reports-bot-design.md`.
