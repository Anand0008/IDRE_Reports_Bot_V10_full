# IDRE Reports Bot — V10

Natural-language interface to the IDRE dispute resolution platform's case, payment, and arbitration data. Staff ask questions in plain English; the bot routes each query to one of two paths:

- **Known-report path** — query maps to one of 12 hardcoded IDRE report endpoints → bot calls IDRE's `/api/reports/*` directly and returns the response (15/15 byte-equal PASS baseline).
- **Derived-query path** — query needs composition beyond a single report → 14-agent LangGraph pipeline generates MySQL against the same database IDRE uses.

V10 retires the V7 RAG (ChromaDB + embeddings), the V6 metric_cards/sql_templates fast paths, and the V9 runtime verifier. Knowledge is regenerated from staging via a build-time pipeline (see `scripts/build_knowledge/`) — no inference-time embedding model.

For the full design contract see `local/docs/superpowers/specs/2026-05-15-v10-reports-bot-design.md`.

## Architecture

```
User NL query
   │
   ▼
harness_entrypoint.run_query_v10 / app.py
   │
   ├── tracing: root span v10.query (OTEL → Jaeger localhost:16686)
   │
   ▼
agents/router.py:route()
   │  • 12 deterministic signatures in config/route_signatures.json
   │  • Word-boundary aware scoring, multi-trigger requirement,
   │    count-intent demote (Tier 1 fix 2026-05-19)
   │  • No LLM call in stage 1 (stage-2 LLM fallback is deferred)
   │
   ├──[known]─► agents/idre_api_client.py
   │              → HTTP GET /api/reports/<id>?<params>
   │              → 60s response cache, auto-login via /api/dev/auto-login
   │              → response_normalizer normalises {data:{rows:[...]}} shapes
   │
   └──[derived]─► core/orchestrator.py (LangGraph state machine)
                     │
                     ▼ 14 agent nodes + 3 utility nodes
                     • context_loader (entity extraction, anaphora resolution)
                     • feedback_injector (conditional, on retry)
                     • ambiguity_scorer + clarification_agent
                     • schema_mapper ∥ platform_context (ThreadPool)
                     • schema_verifier (live SHOW COLUMNS)
                     • sql_writer (Gemini 2.5 Pro + 8 in-process tools)
                     • sql_validator (deterministic safety + column existence)
                     • executor (SQL exec, :now binding, 100K row cap)
                     • debugger (loop: max 3 retries)
                     • post_processor (computed columns, EST urgency)
                     • output_formatter (cells: currency/date/locale)
                     • response_formatter (layout: chart selection + narrative)
                     • audit_trail (async)
```

## The 7 SQL Writer tools (in-process, NOT MCP)

`tools/idre_tools.py` registers 7 Python functions as Gemini function-calling tools:

| Tool | Reads from / does |
|---|---|
| `get_idre_business_logic` | `knowledge/v10/business_logic.json` (Prisma + JS + SQL equivalent per report) |
| `get_table_schema` | `knowledge/v10/schema_catalog.json` |
| `get_enum_values` | `knowledge/v10/enum_catalog.json` (prefer RDS-sampled values) |
| `lookup_business_term` | `config/business_glossary.json` |
| `list_available_reports` | report catalog |
| `find_filter_pattern` | NL date phrase → SQL fragment with `:now` placeholder |
| `verify_sql_executes` | EXPLAIN + LIMIT 5 dry-run on read replica |

`:now` is bound to a MySQL DATETIME literal by `agents/executor._bind_now()` immediately before query execution, using `state.now_anchor_iso` (locked at request start for harness/test parity).

## Roles (access_control.json)

| Code | Role | Tables |
|---|---|---:|
| MA | Master Admin | full (~30) |
| PA | Payment Admin | financial full (~24) |
| PS | Payment Specialist | payment-focused subset |
| AC | Accounting | balance / transactions / reconciliation |
| AM | Arbitrator Manager | team / cases / decisions |
| CB | Capitol Bridge Admin | outstanding payments + payouts |
| VT | VeraTru Support | read-only payment slice |
| VO | View Only (default) | reports without financial detail |
| DQD | Data Quality Debugger | full reporting tables, no auth tables |

System/auth tables (`account`, `session`, `verification`, `twoFactor`, `_prisma_migrations`, etc.) are blocked for all roles.

## Test harness (out of this repo)

The harness lives at `<HOME>\Downloads\local\testing\v10_harness\`. Three categories:

| Suite | Tests | Baseline |
|---|---:|---|
| Known-report (`test_baseline_known.py`) | 15 | 15/15 PASS |
| Derived-UI (`test_baseline_derived_ui.py`) | 15 | 15/15 PASS on staging snapshot |
| Derived-DOM Phase 1 (`test_baseline_derived_dom.py`) | 30 | 7/30 PASS — 20 IDRE render-cost failures, 1 router miscall (fixed), 2 canonical_sql diffs (under investigation) |

OTel traces go to local Jaeger at http://localhost:16686 (service `v10-bot`).

## Project layout

```
v10_reports_bot/
├── agents/           # 17 .py — each is a LangGraph node (14 derived-path + 3 known-path adapters)
├── core/orchestrator.py
├── harness_entrypoint.py
├── app.py            # Streamlit UI (developer toggles, feedback, saved queries)
├── tools/idre_tools.py
├── config/
│   ├── access_control.json
│   ├── business_glossary.json    (v2.1, 80+ terms)
│   ├── business_rules.json       (pricing eras)
│   └── route_signatures.json     (12 known reports)
├── knowledge/v10/
│   ├── manifest.json
│   ├── schema_catalog.json       (built from staging schema.prisma)
│   ├── business_logic.json       (per-report Prisma + JS + SQL)
│   ├── enum_catalog.json         (TS enums × RDS-sampled distinct values)
│   └── report_reference_cards.json
├── state/context.py              # GraphState TypedDict
├── tracing.py                    # OTEL helper (@trace_agent, traced_tool_call, redact)
├── db/connector.py               # SQLAlchemy engine factory
├── utils/                        # glossary, joins, perms, audit, feedback, query cache, query store
├── tests/                        # pytest unit tests (test_tracing, test_router, test_context_loader, test_glossary_matcher, test_feedback_injector)
└── cdk_deploy/                   # AWS CDK stack for EC2 + Streamlit deploy
```

## Setup

```bash
# From v10_reports_bot/
py311 -m pip install -r requirements.txt
cp .env.example .env  # fill in DB creds + Gemini_API_Key

# Run Streamlit UI
streamlit run app.py
# → http://localhost:8501

# Run from CLI / harness
py311 -c "from harness_entrypoint import run_query_v10; print(run_query_v10('show me the dashboard overview'))"

# Run unit tests
py311 -m pytest tests/ -v
```

## Environment variables

| Var | Default | Purpose |
|---|---|---|
| `Gemini_API_Key` | — | Required. Gemini API key. |
| `DB_HOST` / `DB_PORT` / `DB_NAME` / `DB_USER` / `DB_PASSWORD` | — | MySQL read-only creds for staging RDS |
| `DB_SSL_CA` | `./global-bundle.pem` | AWS RDS SSL bundle |
| `DB_READ_REPLICA_HOST` | unset | If set, analytical queries route to this host |
| `APP_PASSWORD` | — | Required by app.py auth gate |
| `V10_OTEL_ENABLED` | `1` | `0` → decorators become no-ops (zero overhead) |
| `V10_ROW_CAP` | `100000` | Production row cap |
| `V10_DISABLE_ROW_CAP` | unset | Test runs only — disables row cap |
| `V10_AMBIGUITY_THRESHOLD` | unset | Override scorer/clarification threshold (1.0 = disable gate) |
| `IDRE_BASE_URL` | `http://127.0.0.1:3000` | IDRE local URL for known-path calls |

## Where to read next

- `local/docs/superpowers/specs/2026-05-15-v10-reports-bot-design.md` — full V10 design spec
- `local/docs/superpowers/reports/2026-05-19-handoff-architecture-review.md` — current architectural state
- `local/docs/superpowers/reports/2026-05-19-router-tier1-applied.md` — router Tier 1 fix details
- `local/docs/superpowers/reports/2026-05-19-node2-context-loader-applied.md` — Node 2 deepdive details
- `local/docs/superpowers/plans/2026-future-router-option-f.md` — pending router enhancement
