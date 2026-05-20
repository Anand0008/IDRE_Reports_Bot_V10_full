# data/

Runtime state files. All are gitignored — see `.gitignore` at the repo root.

## Active in V10

| File | Written by | Purpose |
|---|---|---|
| `audit_log.jsonl` | `utils/audit_writer.py` | One JSON-line per query: prompt, resolved query, role, SQL, status, latency, tokens. Async, append-only. Consumed by `utils/audit_analytics.py` for the developer sidebar panel. |
| `feedback_log.jsonl` | `utils/feedback_store.py` | One JSON-line per feedback submission (correct / incorrect / retry). Async, append-only. Consumed by `utils/feedback_analytics.py`. |
| `saved_queries.json` | `utils/query_store.py` | Cross-session "save this as X" → "run X" feature. Keyed by lowercase name. |
| `error_knowledge_base.json` | `agents/debugger_agent.py` | Per-error_type history of fixes + success flags. Capped at 20 entries per error_type. Read by future debugger calls on the same error_type. |
| `query_frequency.json` | `agents/executor.py` | SQL-hash → call count. Triggers materialization after 3+ calls on the same query (size >100 rows). |
| `anomaly_window.json` | `utils/audit_writer.py` | Sliding window for anomaly detection (failure spikes per hour, repeated-query loops, clarification loops). |
| `schema_snapshot.json` | `agents/schema_verifier.py` | Per-table column lists from the last successful `SHOW COLUMNS` call. Used for schema-drift detection. |
| `materialized_results/` | `agents/executor.py` | Cached query results (TTL 1h) for SQL hashes that have been seen ≥3 times. JSON files keyed by hash. |

## Removed in Node 3 (clarification + ambiguity deepdive)

| File | Status |
|---|---|
| `clarification_history.json` | Writes removed. The `_find_auto_answer` / `_save_clarification_answer` feature was structurally broken since V6 (entries written with empty `user_answer`); removed in Node 3 per V10 spec §8.7 #4. |
| `ambiguity_calibration.jsonl` | Writes removed. The append-only score log had no consumer at runtime — unbounded growth removed in Node 3. |

## Inactive / legacy

| File / Dir | Status | Notes |
|---|---|---|
| `chroma_db/` | **Inert leftover from V7 RAG.** | V10 spec §3 retired ChromaDB. No V10 code references it. The directory + sqlite file remain on disk because deletion is irreversible and harmless to leave. Will be removed in a future cleanup pass. Excluded from git via `.gitignore`. |
| `table_cooccurrence.json` | **Never existed on disk in any version.** | V6 introduced `_load_cooccurrence` / `save_cooccurrence` / `_boost_cooccurring` in `agents/schema_mapper.py` but `save_cooccurrence` had no caller — the file was never written. All 4 functions + the `COOCCURRENCE_PATH` constant were removed in Node 4. |

## Schema catalog dual-path (carried forward to Node 5)

The repo has TWO `schema_catalog.json` files:

| Path | Shape | Source | Consumers |
|---|---|---|---|
| `schema_catalog.json` (repo root, 308 KB) | `{database, host, table_count, tables: {<name>: {columns, foreign_keys, sample_values, ...}}}` | `analyze_db.py` — runs `SHOW COLUMNS`/`SHOW INDEX`/`information_schema` against MySQL staging | `agents/schema_mapper.py`, `agents/sql_validator.py`, `utils/join_graph.py` (anything that needs FKs or sample values) |
| `knowledge/v10/schema_catalog.json` (218 KB) | `{idre_git_sha, models: [{model, table_name, columns: [{name, type, optional, is_list, attributes}]}], enums_inline, model_count, enum_count}` | `local/scripts/build_knowledge/` pipeline — parses IDRE Prisma schema and route handlers | `tools/idre_tools.py:get_table_schema` (LLM-side tool) |

Both files coexist intentionally for now:
- Root file has the FK graph + sample values needed by pre-LLM agents.
- v10 file has the freshest Prisma model list (53 models) + Prisma attributes needed by the LLM tool calls.

### Unification roadmap (Design A, deferred to Phase 5)

Node 5 (2026-05-20) evaluated 5 unification designs (see `local/docs/superpowers/reports/2026-05-20-node5-architecture-decision.md`) and selected **Design E**: dead-code cleanup now + Design A as the future direction.

**Design A — Regenerate root from Prisma + MySQL probe (target for Phase 5):**
- Extend `local/scripts/build_knowledge/03_extract_schema.py` to also parse Prisma `@relation()` text into structured `foreign_keys` records.
- Add a one-time MySQL probe step to populate `sample_values` per column (currently only `analyze_db.py` does this, manually).
- Wire the regenerated root file into `run_all.py` so it refreshes alongside `business_logic.json` and `enum_catalog.json` on every pipeline run.
- After this lands, manual `analyze_db.py` runs can be retired.

**Why this design (vs B / C):**
- Design B (extend v10 file, refactor 5 consumers) touches `utils/join_graph.py`, byte-identical across 8 generations — high cost for stable code.
- Design C (adapter layer) loses FK + sample-values data unless we backfill them, at which point it's just Design B with extra steps.
- Design A keeps both files but unifies their source (one pipeline run produces both shapes).

**Current Phase 5 prerequisites:**
- IDRE Prisma schema must be readable at `local/idre/prisma/schema.prisma` (already the case).
- MySQL staging must be reachable for the sample-values probe (already the case via `db/connector.py`).
- No new dependencies.

**Refresh-without-pipeline:** Until Phase 5 lands, root file can be manually refreshed by running `py -3.11 analyze_db.py` from `v10_reports_bot/`. The current root file dates from Apr 9, 2026 (308 KB, 44 tables); Prisma now has 53 tables, so 9 production tables aren't currently in the BM25 corpus or join graph. Re-running `analyze_db.py` would close that gap.

## Adding new state files

- Append the gitignore entry first.
- If the file is consumed at runtime (vs append-only telemetry), document the schema in the writing module's docstring.
- If the file accumulates without bound, set a cap or rotation policy.
