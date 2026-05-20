from typing import Optional, List, Any, Dict
from typing_extensions import TypedDict


class GraphState(TypedDict):
    """State object that flows through the LangGraph pipeline — V10.

    Every agent in core.orchestrator reads from this state and returns
    a (possibly extended) version. Fields are grouped by section and
    documented inline.

    History:
    - V6: base shape (user_query through audit fields), 9-role
      access_control, entity_registry, query_complexity,
      self_verification, explain_plan.
    - V8: removed embedding-related fields (none retained on this
      type, embeddings never had a state field of their own).
    - V10 additions:
      - now_anchor_iso: ISO 8601 UTC, locked at request start, used by
        executor._bind_now to substitute :now in SQL.
      - knowledge_git_sha: SHA of knowledge/v10/ the bot is running
        against (read from manifest.json once at startup).
      - was_capped: True if executor truncated rows at production cap;
        test runs that hit this should FAIL regardless of value
        correctness (per V10 spec §6.3).
    - Known-path removal (2026-05-20): dropped `router_decision` and
      `idre_api_response` fields. See
      `local/docs/superpowers/specs/2026-05-20-known-path-removal-design.md`.
    """
    user_query: str
    session_id: str

    # Access control
    user_role: str              # 'MA' | 'PA' | 'PS' | 'AC' | 'AM' | 'CB' | 'VT' | 'VO' | 'DQD'
    permitted_tables: List[str] # resolved from access_control.json by Context Loader

    # Session memory
    conversation_history: List[Dict[str, str]]  # [{query, summary}, ...] last N turns
    resolved_query: str                         # user_query after pronoun/reference expansion

    # V6: Entity registry for context loader
    entity_registry: Dict[str, str]

    # Schema mapping
    relevant_tables: List[str]
    schema_context: str

    # Platform knowledge context (injected by platform_context_agent)
    platform_context: str

    # SQL generation
    generated_sql: str
    validated_sql: str

    # Execution
    query_result: Optional[List[Dict[str, Any]]]
    row_count: int
    execution_error: Optional[str]

    # Ambiguity scoring
    ambiguity_score: float
    ambiguity_flags: List[str]

    # Business Glossary
    glossary_matches: List[Dict[str, Any]]

    # Clarification
    needs_clarification: bool
    clarification_question: str
    clarification_attempted: bool

    # Response
    formatted_response: str
    assumptions: List[str]
    response_format: str
    chart_config: Optional[Dict[str, Any]]
    query_explanation: str
    query_narrative: str
    proactive_suggestions: List[str]

    # Pipeline control
    retry_count: int
    error_message: str
    retry_context: str
    # Node 8 (2026-05-20): declared explicitly so LangGraph's TypedDict-
    # based state merge preserves them across the debugger → routing →
    # increment_retry / max_retry_error transitions. Previously dropped
    # silently — root cause of smoke-test S6 (debugger 'abort' strategy
    # not honoured by _route_after_debugger). See
    # local/docs/superpowers/reports/2026-05-20-node8-executor-debugger-audit.md
    debug_error_type: str
    debug_retry_strategy: str

    # V6: Query complexity scoring
    query_complexity: Optional[Dict[str, Any]]

    # Audit / timing
    pipeline_start_ms: int
    agent_timings: Dict[str, int]

    # Token usage
    token_usage: Dict[str, Any]

    # Structured trace
    agent_trace: List[Dict[str, Any]]

    # Feedback & Reproducibility
    user_identity: str
    feedback_correction_context: Optional[Dict[str, Any]]
    is_feedback_retry: bool
    feedback_record_id: str

    # V6: User preferences
    user_preferences: Optional[Dict[str, Any]]

    # V6: SQL self-verification result
    self_verification: Optional[Dict[str, Any]]

    # V6: EXPLAIN plan preview
    explain_plan: Optional[Dict[str, Any]]

    # ─── V10 additions ───
    now_anchor_iso: str          # ISO 8601 timestamp locked at request start
    knowledge_git_sha: str       # SHA of the knowledge/v10 in use
    was_capped: bool             # True if executor truncated rows at production cap
    # 2026-05-21 (persistent-logging): every tool call across every
    # sql_writer attempt accumulates here. Audit + feedback writers
    # capture this for full reproducibility.
    tool_calls_log: List[Dict[str, Any]]
