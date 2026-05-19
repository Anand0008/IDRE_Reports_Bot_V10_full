"""Unit tests for utils/glossary_matcher.py."""
from __future__ import annotations
import sys
from pathlib import Path

_BOT_ROOT = Path(__file__).parent.parent.resolve()
if str(_BOT_ROOT) not in sys.path:
    sys.path.insert(0, str(_BOT_ROOT))

from utils.glossary_matcher import find_matches, format_glossary_context  # noqa: E402


def test_find_matches_simple_term():
    matches = find_matches("how many open cases?")
    terms = [m["term"] for m in matches]
    assert "open case" in terms


def test_find_matches_alias():
    """Match an alias, not the primary term."""
    matches = find_matches("how many active disputes do we have?")
    terms = [m["term"] for m in matches]
    # "active dispute" is an alias of "open case" per business_glossary.json
    assert "open case" in terms


def test_find_matches_specific_beats_generic():
    """Longer phrase should rank higher than shorter."""
    matches = find_matches("show me pending second payment cases")
    terms = [m["term"] for m in matches]
    # "pending second payment" is more specific than "pending payment"
    if "pending second payment" in terms and "pending payment" in terms:
        idx_specific = terms.index("pending second payment")
        idx_generic = terms.index("pending payment")
        assert idx_specific < idx_generic, (
            "more-specific glossary term should sort first"
        )


def test_find_matches_no_match():
    matches = find_matches("xyzqwertyuiop")
    assert matches == []


def test_find_matches_empty_query():
    matches = find_matches("")
    assert matches == []


def test_format_glossary_context_skips_null_filter():
    """Terms without a sql_filter (instruction-only entries) should be
    excluded from the SQL-fragment block."""
    # Construct minimal entries
    entries = [
        {"term": "dispute number", "definition": "the ID", "sql_filter": None,
         "applies_to_tables": ["case"], "requires_join": False, "join_table": None},
        {"term": "open case", "definition": "...", "sql_filter": "x = 1",
         "applies_to_tables": ["case"], "requires_join": False, "join_table": None},
    ]
    out = format_glossary_context(entries)
    assert "open case" in out
    assert "dispute number" not in out


def test_format_glossary_context_empty():
    assert format_glossary_context([]) == ""
