"""
Schema Mapper Agent — V10 (post-Node-4)

Picks the relevant subset of tables for the user query. Pure BM25
ranking over compiled per-table documents (name + description +
columns + foreign keys + sample values), plus intent-detection
regexes that force-include certain tables (org_name → organization
+ case_party; payment terms → payment + case_payment_allocation;
NIP/IP → case_party + organization). Dynamic top-K (3/5/6/8) based
on query word count + connector density. Filters against
permitted_tables for the role.

Reads `schema_catalog.json` at the repo root (MySQL-derived: has FKs
and sample values). A second Prisma-derived catalog at
`knowledge/v10/schema_catalog.json` is read by the LLM tools
(`tools/idre_tools.py`) — it has the freshest model list but no FKs
or sample values. Dual-path is documented in `data/README.md`;
unification is deferred to Node 5.

NO vector embeddings. NO ChromaDB.

History:
- V6: BM25 baseline + ChromaDB hybrid + RRF + cooccurrence boost.
- V8: removed ChromaDB; left `_rrf_merge` orphaned and the
  cooccurrence functions inert (input file never created).
- V10 Node 4 (2026-05-20): removed `_rrf_merge`,
  `save_cooccurrence`, `_load_cooccurrence`, `_boost_cooccurring`,
  `COOCCURRENCE_PATH` (all dead for 4+ generations). See
  `local/docs/superpowers/reports/2026-future-node4-history-summary.md`.
"""
import json
import os
import re
from collections import Counter
from typing import List
from state.context import GraphState
from utils.join_graph import get_join_context, get_graph_stats
from utils.glossary_matcher import format_glossary_context
from tracing import trace_agent

# MySQL-derived catalog (FKs + sample values). Paired with the
# Prisma-derived `knowledge/v10/schema_catalog.json` used by the LLM
# tools — see data/README.md for the dual-path explanation.
SCHEMA_CATALOG_PATH = os.path.join(os.path.dirname(__file__), "..", "schema_catalog.json")

_catalog_docs_cache = None

TABLE_DESCRIPTIONS = {
    "case": "Core dispute case entity. Each row is one IDR dispute filing. Key fields: status (case lifecycle), disputeType (SINGLE/BUNDLED/BATCHED), disputeNumber (7-digit ID), due_date, due_date_until_decision, createdAt, closureReason, assignedToId (arbitrator). Links to organizations via initiatingPartyOrganizationId and nonInitiatingPartyOrganizationId.",
    "case_action": "Audit trail of all case events/status changes. actionType='STATUS_CHANGED' tracks lifecycle transitions (fromValue → toValue). Also tracks NOTE_ADDED, DOCUMENT_UPLOADED, ASSIGNMENT_CHANGED, etc.",
    "case_party": "Contact information for a party in a case (name, address, phone, email, fax). partyType is 'PROVIDER' or 'HEALTH_PLAN'. To get IP contact: JOIN ON case.initiatingPartyId = case_party.id. To get NIP contact: JOIN ON case.nonInitiatingPartyId = case_party.id.",
    "case_payment_allocation": "Junction table linking payments to cases. Each row allocates an amount from a payment to a specific case+party. Key: caseId, paymentId, partyType, allocatedAmount.",
    "case_refunds": "Refund records tied to case outcomes. Tracks entity fee refunds to prevailing parties after arbitration decision.",
    "payment": "All financial transactions. Key fields: amount, status (PENDING/ON_HOLD/APPROVED/COMPLETED/CANCELLED/FAILED), direction (INCOMING/OUTGOING), type (CASE_PAYMENT/REFUND_TO_PREVAILING_PARTY/CAPITOL_BRIDGE_FEE/THIRD_PARTY_PAYMENT/CMS_INVOICE_PAYMENT/CMS_ADMIN_FEE_TRANSFER/PARTY_REFUND_IP/PARTY_REFUND_NIP), nachaBatchId (FK to nacha_batch), paidAt, processedAt.",
    "arbitration_decision": "Arbitration decisions per case. decisionType (CASE_LEVEL/LINE_ITEM_LEVEL), awardRecipient (INITIATING_PARTY/NON_INITIATING_PARTY/SPLIT_DECISION), renderedAt, reasoning.",
    "line_item_decision": "Per-line-item arbitration decisions. Links to arbitration_decision and dispute_line_items.",
    "dispute_line_items": "Individual line items within a dispute. Each represents a specific claim/charge being disputed.",
    "organization": "Organizations (healthcare providers and health plans). name, type, createdAt. Parent entity for parties in cases.",
    "member": "Organization members with roles (owner, admin, member). Links users to organizations.",
    "invoice": "Billing invoices for case fees. invoiceNumber, totalAmount, dueDate, status (PENDING/SENT/PAID/OVERDUE/CANCELLED).",
    "invoice_item": "Individual line items within an invoice. Links to cases via caseId.",
    "invoice_payment": "Payments against invoices. Tracks varianceType (OVERPAYMENT/UNDERPAYMENT/EXACT) and varianceAmount.",
    "cms_invoice": "CMS (Centers for Medicare & Medicaid) fee invoices. status (RECEIVED/VALIDATED/PROCESSED/DISCREPANCY/REJECTED).",
    "cms_invoice_payment_allocation": "Allocates CMS invoice payments to cases.",
    "nacha_batch": "ACH batch files for bulk payment processing. Groups multiple payments into a single NACHA file.",
    "bank_account": "Bank accounts for organizations. achStatus (PENDING/APPROVED/REJECTED/REQUIRES_VERIFICATION).",
    "bank_account_approval": "Approval workflow for bank account verification.",
    "payment_approval": "Approval records for individual payments.",
    "payment_reminder": "Payment reminder records sent to parties about pending fees.",
    "payment_reminder_log": "Log of reminder emails sent, tracking delivery status.",
    "email_job": "Email notification queue. Tracks all platform emails: type, recipient, status (pending/sent/failed).",
    "case_note": "Internal notes attached to cases by staff.",
    "case_document": "Documents uploaded or attached to cases.",
    "case_documentation_checklist": "Tracks completion of required documentation (Notice of Offer, NPI, TIN, etc.).",
    "case_contact": "Contact information for parties involved in a case.",
    "case_ach_info": "ACH payment information specific to a case.",
    "case_party_payment_lock": "Locks preventing duplicate payment processing for a case party.",
    "global_organization_member": "Cross-organization role assignments for admin users.",
    "invoice_number_audit_log": "Audit log for invoice number generation and changes.",
}


def _build_table_document(table_name: str, table_info: dict) -> str:
    parts = [f"Table: {table_name}"]
    desc = TABLE_DESCRIPTIONS.get(table_name)
    if desc:
        parts.append(f"Description: {desc}")
    cols = table_info.get("columns", [])
    if cols:
        col_strs = [f"{c['name']} ({c['type']})" for c in cols]
        parts.append("Columns: " + ", ".join(col_strs))
    fks = table_info.get("foreign_keys", [])
    if fks:
        fk_strs = [f"{fk['column']} -> {fk['references_table']}.{fk['references_column']}" for fk in fks]
        parts.append("Foreign keys: " + ", ".join(fk_strs))
    sample = table_info.get("sample_values", {})
    if sample:
        sample_strs = []
        for col, vals in list(sample.items())[:4]:
            if vals:
                sample_strs.append(f"{col}: {', '.join(str(v) for v in vals[:3])}")
        if sample_strs:
            parts.append("Sample values — " + "; ".join(sample_strs))
    return "\n".join(parts)


def _get_catalog_docs() -> dict[str, str]:
    global _catalog_docs_cache
    if _catalog_docs_cache is not None:
        return _catalog_docs_cache
    with open(SCHEMA_CATALOG_PATH) as f:
        catalog = json.load(f)
    _catalog_docs_cache = {}
    for table_name, table_info in catalog["tables"].items():
        _catalog_docs_cache[table_name] = _build_table_document(table_name, table_info)
    return _catalog_docs_cache


## ChromaDB / vector search removed in V8 — using BM25 + intent detection only


# ── V6: BM25 scorer ─────────────────────────────────────────────────────────

def _bm25_score(query: str, doc: str, k1: float = 1.5, b: float = 0.75, avg_dl: float = 200) -> float:
    query_terms = set(query.lower().split())
    doc_terms = doc.lower().split()
    dl = len(doc_terms)
    tf_map: Counter = Counter(doc_terms)
    score = 0.0
    for term in query_terms:
        tf = tf_map.get(term, 0)
        if tf > 0:
            idf = 1.0
            numerator = tf * (k1 + 1)
            denominator = tf + k1 * (1 - b + b * dl / avg_dl)
            score += idf * numerator / denominator
    return score


def _bm25_rank(query: str, table_docs: dict[str, str]) -> list[tuple[str, float]]:
    avg_dl = sum(len(d.split()) for d in table_docs.values()) / max(len(table_docs), 1)
    scored = []
    for table_name, doc in table_docs.items():
        score = _bm25_score(query, doc, avg_dl=avg_dl)
        scored.append((table_name, score))
    scored.sort(key=lambda x: -x[1])
    return scored


# ── V6: Dynamic TOP_K ────────────────────────────────────────────────────────

_COMPLEX_KEYWORDS = re.compile(
    r"\b(and|with|including|along with|also|plus|combined|both|"
    r"join|compare|versus|vs|between|correlation)\b", re.IGNORECASE)
_SIMPLE_KEYWORDS = re.compile(
    r"^(how many|count|total number of|what is the)\b", re.IGNORECASE)


def _compute_dynamic_k(query: str) -> int:
    word_count = len(query.split())
    complex_matches = len(_COMPLEX_KEYWORDS.findall(query))
    if _SIMPLE_KEYWORDS.match(query.strip()) and word_count <= 8:
        return 3
    if word_count <= 6 and complex_matches == 0:
        return 3
    if complex_matches >= 2 or word_count >= 20:
        return 8
    if complex_matches >= 1 or word_count >= 12:
        return 6
    return 5


# ── V6: Query decomposition ─────────────────────────────────────────────────

def _decompose_query(query: str) -> list[str]:
    parts = re.split(r'\b(?:AND|,)\b(?![^(]*\))', query, flags=re.IGNORECASE)
    parts = [p.strip() for p in parts if p.strip() and len(p.strip()) > 5]
    if len(parts) <= 1:
        return [query]
    return parts


# ── Intent detection ─────────────────────────────────────────────────────────

_ORG_NAME_PATTERN = re.compile(
    r"\b(uhc|united\s*health\s*care|unitedhealth|halomd|halo\s*md|pacifichealth|"
    r"pacific\s*health|capitol\s*bridge|veratru|vera\s*tru|aetna|cigna|anthem|"
    r"humana|bcbs|blue\s*cross|kaiser|molina|centene|radix|"
    r"organization|org\s+name)\b", re.IGNORECASE)
_PERSON_NAME_PATTERN = re.compile(
    r"\b(assigned\s+to|closed\s+by|arbitrator|specialist|"
    r"[A-Z][a-z]+\s+[A-Z][a-z]+)\b")
_DISPUTE_NUM_PATTERN = re.compile(r"\bDISP-\w+\b", re.IGNORECASE)
_PAYMENT_PATTERN = re.compile(
    r"\b(payment|paid|unpaid|refund|amount|fee|invoice|nacha|ach|"
    r"disbursement|payout|allocation|balance|fund)\b", re.IGNORECASE)
_NIP_IP_PATTERN = re.compile(
    r"\b(NIP|non.initiating|initiating\s+party|IP\s+|health\s+plan|provider|respondent|"
    r"filing\s+party|claimant)\b", re.IGNORECASE)


def _detect_intent_tables(query: str) -> List[str]:
    forced = ["case"]
    if _ORG_NAME_PATTERN.search(query):
        forced.extend(["organization", "case_party"])
    if _PERSON_NAME_PATTERN.search(query):
        forced.append("user")
    if _DISPUTE_NUM_PATTERN.search(query):
        forced.extend(["case_party", "organization"])
    if _PAYMENT_PATTERN.search(query):
        forced.extend(["payment", "case_payment_allocation"])
    if _NIP_IP_PATTERN.search(query):
        forced.extend(["case_party", "organization"])
    return list(set(forced))


def get_relevant_tables(query: str, top_k: int = 6) -> List[str]:
    catalog_docs = _get_catalog_docs()

    sub_queries = _decompose_query(query)
    bm25_scores: dict[str, float] = {}
    for sq in sub_queries:
        for table, score in _bm25_rank(sq, catalog_docs):
            bm25_scores[table] = bm25_scores.get(table, 0) + score

    bm25_ranked = sorted(bm25_scores.items(), key=lambda x: -x[1])
    ranked = [t for t, _ in bm25_ranked]

    intent_tables = _detect_intent_tables(query)
    for t in intent_tables:
        if t not in ranked:
            ranked.insert(0, t)

    return ranked[:top_k]


def build_schema_context(table_names: List[str]) -> str:
    with open(SCHEMA_CATALOG_PATH) as f:
        catalog = json.load(f)
    parts = []
    for name in table_names:
        info = catalog["tables"].get(name)
        if not info:
            continue
        parts.append(_build_table_document(name, info))
    schema_text = "\n\n".join(parts)
    join_hints = get_join_context(table_names)
    if join_hints:
        schema_text += "\n\n" + join_hints
    return schema_text


@trace_agent("v10.agent.schema_mapper")
def schema_mapper_node(state: GraphState) -> GraphState:
    query = state.get("resolved_query") or state["user_query"]

    dynamic_k = _compute_dynamic_k(query)
    tables = get_relevant_tables(query, top_k=dynamic_k)

    permitted = state.get("permitted_tables", [])
    if permitted:
        blocked = [t for t in tables if t not in permitted]
        tables = [t for t in tables if t in permitted]
    else:
        blocked = []

    intent_forced = []
    for tbl in _detect_intent_tables(query):
        if tbl not in tables and (not permitted or tbl in permitted):
            tables = tables + [tbl]
            intent_forced.append(tbl)

    glossary_matches = state.get("glossary_matches", [])
    glossary_forced = []
    for match in glossary_matches:
        if match.get("requires_join") and match.get("join_table"):
            join_tbl = match["join_table"]
            if join_tbl not in tables and (not permitted or join_tbl in permitted):
                tables = tables + [join_tbl]
                glossary_forced.append(join_tbl)
        for tbl in match.get("applies_to_tables", []):
            if tbl not in tables and (not permitted or tbl in permitted):
                tables = tables + [tbl]
                glossary_forced.append(tbl)

    if permitted:
        tables = [t for t in tables if t in permitted]

    schema_ctx = build_schema_context(tables)
    glossary_block = format_glossary_context(glossary_matches)
    if glossary_block:
        schema_ctx = schema_ctx + "\n\n" + glossary_block

    join_hints = get_join_context(tables)
    join_count = join_hints.count("↔") if join_hints else 0
    stats = get_graph_stats()

    detail = list(tables)
    if blocked:
        detail.append(f"Permission-blocked tables (not shown to LLM): {', '.join(blocked)}")
    if intent_forced:
        detail.append(f"Intent-forced tables: {', '.join(set(intent_forced))}")
    if glossary_forced:
        detail.append(f"Glossary forced tables: {', '.join(set(glossary_forced))}")
    if join_count:
        detail.append(f"FK graph: {stats['fk_edges']} edges across {stats['tables']} tables")
    detail.append(f"Dynamic TOP_K: {dynamic_k} (based on query complexity)")

    summary = f"Matched {len(tables)} tables (BM25 + intent regex) · {join_count} join path(s) · K={dynamic_k}"
    if blocked:
        summary += f" · {len(blocked)} table(s) blocked by role"

    trace_entry = {
        "agent": "Schema Mapper",
        "status": "ok",
        "summary": summary,
        "detail": detail,
    }
    trace = state.get("agent_trace", []) + [trace_entry]

    # OTEL custom attributes (Risk R2 mitigation: best-effort score extraction)
    try:
        from opentelemetry import trace as _otel_trace
        span = _otel_trace.get_current_span()
        if span is not None:
            try:
                span.set_attribute("schema.tables_matched", [t for t in tables][:10])
                span.set_attribute("schema.k", len(tables))
                # get_relevant_tables returns names only; scores not exposable here
                scores = state.get("table_scores")
                if scores and isinstance(scores, list):
                    span.set_attribute("schema.scores", [round(float(s), 4) for s in scores[:10]])
                    span.set_attribute("schema.scores_available", True)
                else:
                    span.set_attribute("schema.scores_available", False)
                if blocked:
                    span.set_attribute("schema.blocked_count", len(blocked))
                if intent_forced:
                    span.set_attribute("schema.intent_forced_count", len(intent_forced))
            except Exception as _e:
                span.set_attribute("schema.attr_error", str(_e)[:200])
    except Exception:
        pass

    return {**state, "relevant_tables": tables, "schema_context": schema_ctx, "agent_trace": trace}
