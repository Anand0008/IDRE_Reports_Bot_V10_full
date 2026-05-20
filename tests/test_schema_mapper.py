"""Tests for agents/schema_mapper.py."""
import agents.schema_mapper as sm


def test_dead_v6_cooccurrence_functions_removed():
    """V6 RRF + cooccurrence functions removed in Node 4.

    History: V6 introduced these for hybrid vector+BM25 fusion.
    V8 removed ChromaDB but left these defs orphaned. table_cooccurrence.json
    was never populated in any version. See:
    local/docs/superpowers/reports/2026-future-node4-history-summary.md
    """
    assert not hasattr(sm, "_rrf_merge"), "_rrf_merge orphaned since V8 — must be removed"
    assert not hasattr(sm, "save_cooccurrence"), "save_cooccurrence never had a caller — must be removed"
    assert not hasattr(sm, "_load_cooccurrence"), "_load_cooccurrence input file never existed — must be removed"
    assert not hasattr(sm, "_boost_cooccurring"), "_boost_cooccurring is permanent no-op — must be removed"
    assert not hasattr(sm, "COOCCURRENCE_PATH"), "COOCCURRENCE_PATH constant has no consumer — must be removed"


def test_no_math_import():
    """`math` import was only used by removed code; should be gone."""
    import inspect
    source = inspect.getsource(sm)
    # `import math` is on its own line in the imports block
    assert "\nimport math\n" not in source, "math import unused after dead-code removal"
