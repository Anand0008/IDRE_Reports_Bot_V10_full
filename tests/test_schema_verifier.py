"""Tests for agents/schema_verifier.py."""
import inspect
import os

import agents.schema_verifier as sv


def test_dead_node4_vintage_helpers_removed():
    """`suggest_column` + `_levenshtein` orphaned since Node 2/3 cleanup
    removed the `_COMMONLY_HALLUCINATED` SUGGESTION path. `_load_column_usage`
    is a permanent no-op (input file never written in any version).
    Removed in Node 5 per:
    local/docs/superpowers/reports/2026-05-20-node5-architecture-decision.md
    """
    assert not hasattr(sv, "suggest_column"), "suggest_column orphaned since Node 2/3 — must be removed"
    assert not hasattr(sv, "_levenshtein"), "_levenshtein only used by suggest_column — must be removed"
    assert not hasattr(sv, "_load_column_usage"), "_load_column_usage reads a file never written — must be removed"
    assert not hasattr(sv, "_COLUMN_USAGE_PATH"), "_COLUMN_USAGE_PATH no consumer after _load_column_usage removed"


def test_no_frequently_used_annotation():
    """The 'frequently used (Nx)' column annotation depended on
    _load_column_usage; removing it leaves the line dead in build_verified_schema.
    """
    source = inspect.getsource(sv)
    assert "frequently used" not in source, "frequently-used annotation must be removed"
    assert "column_usage" not in source.lower(), "no column_usage references should remain"


def test_existing_capabilities_preserved():
    """Sanity: live SHOW COLUMNS, SHOW INDEX, schema-diff detection,
    INDEXED annotation must remain — these are still used."""
    assert hasattr(sv, "_fetch_columns")
    assert hasattr(sv, "_fetch_indexes")
    assert hasattr(sv, "_detect_schema_diff")
    assert hasattr(sv, "build_verified_schema")
    assert hasattr(sv, "schema_verifier_node")
    source = inspect.getsource(sv)
    assert "INDEXED" in source, "INDEXED column annotation must remain"
    assert "SCHEMA CHANGES DETECTED" in source, "schema-diff detection must remain"
