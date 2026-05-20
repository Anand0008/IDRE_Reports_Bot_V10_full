"""Tests for agents/executor.py (Node 8).

Pins the V10-critical executor behaviour:
- Phase 2 (I2 / smoke S4): `_bind_runtime_params` handles both `:now`
  and `:current_user_id` with the safety regex.
- 100K row cap (`_enforce_limit`), env-override path.
- SQL hash stability + analytical-query routing predicate.
- Materialized-cache TTL + frequency-tracking primitives.

Live DB execution paths are not covered here — that's smoke-test territory.
"""
import json
import os
import time
import importlib

import agents.executor as ex


# ── Phase 2 (I2): _bind_runtime_params ───────────────────────────────

def test_bind_runtime_params_substitutes_now_from_state():
    state = {"now_anchor_iso": "2026-05-20T04:01:23.999Z", "user_identity": ""}
    sql = ex._bind_runtime_params(
        "SELECT * FROM `case` WHERE createdAt >= :now",
        state,
    )
    assert ":now" not in sql
    # MySQL DATETIME literal — fractional + timezone stripped
    assert "'2026-05-20 04:01:23'" in sql


def test_bind_runtime_params_now_fallback_when_anchor_missing():
    """When state.now_anchor_iso is empty, fall back to UTC_TIMESTAMP()
    so queries still run — temporal-anchoring guarantees broken but
    behaviour preserved."""
    state = {"now_anchor_iso": "", "user_identity": ""}
    sql = ex._bind_runtime_params("SELECT :now", state)
    assert "UTC_TIMESTAMP()" in sql
    assert ":now" not in sql


def test_bind_runtime_params_substitutes_current_user_id_safe_value():
    """Identity values matching [A-Za-z0-9_-]+ are substituted; the
    substitution is the smoke-test S4 fix."""
    state = {"now_anchor_iso": "", "user_identity": "user_abc123"}
    sql = ex._bind_runtime_params(
        "SELECT id FROM `case` WHERE assignedToId = :current_user_id",
        state,
    )
    assert ":current_user_id" not in sql
    assert "'user_abc123'" in sql


def test_bind_runtime_params_skips_current_user_id_when_unsafe():
    """Defence-in-depth: identity values containing characters outside
    the safe set (quote, semicolon, whitespace) are NOT substituted.
    Placeholder is left intact so SQLAlchemy raises a clear bind error
    instead of risking a malformed/unsafe substitution."""
    state = {"now_anchor_iso": "", "user_identity": "bad'; DROP TABLE x; --"}
    original = "SELECT id FROM `case` WHERE assignedToId = :current_user_id"
    sql = ex._bind_runtime_params(original, state)
    assert sql == original, "Unsafe identity must leave the placeholder alone"


def test_bind_runtime_params_both_placeholders_at_once():
    state = {
        "now_anchor_iso": "2026-05-20T04:01:23Z",
        "user_identity": "auth0_xyz",
    }
    sql = ex._bind_runtime_params(
        "SELECT id FROM `case` "
        "WHERE assignedToId = :current_user_id AND createdAt < :now",
        state,
    )
    assert ":now" not in sql and ":current_user_id" not in sql
    assert "'2026-05-20 04:01:23'" in sql
    assert "'auth0_xyz'" in sql


def test_bind_runtime_params_noop_when_no_placeholders():
    state = {"now_anchor_iso": "2026-05-20T04:01:23Z", "user_identity": "x"}
    sql_in = "SELECT id FROM `case`"
    assert ex._bind_runtime_params(sql_in, state) == sql_in


# ── _enforce_limit: row cap ──────────────────────────────────────────

def test_enforce_limit_adds_limit_to_plain_select():
    out = ex._enforce_limit("SELECT id FROM `case`")
    assert "LIMIT" in out and str(ex.ROW_LIMIT) in out


def test_enforce_limit_preserves_existing_limit():
    out = ex._enforce_limit("SELECT id FROM `case` LIMIT 7")
    # No second LIMIT appended
    assert out.count("LIMIT") == 1
    assert "LIMIT 7" in out


def test_enforce_limit_skips_aggregate_without_group_by():
    """COUNT(*)/SUM/AVG/MIN/MAX without GROUP BY return a single row;
    appending LIMIT 100000 is pointless. Confirm the guard fires."""
    out = ex._enforce_limit("SELECT COUNT(*) FROM `case`")
    assert "LIMIT" not in out.upper()


def test_enforce_limit_aggregate_with_group_by_still_capped():
    """Aggregate + GROUP BY can return many rows → cap should apply."""
    out = ex._enforce_limit("SELECT status, COUNT(*) FROM `case` GROUP BY status")
    assert "LIMIT" in out and str(ex.ROW_LIMIT) in out


def test_enforce_limit_bypassed_when_env_disables(monkeypatch):
    """V10_DISABLE_ROW_CAP=1 is the test-harness escape hatch — must
    not append LIMIT regardless of query shape."""
    monkeypatch.setattr(ex, "DISABLE_ROW_CAP", True)
    out = ex._enforce_limit("SELECT id FROM `case`")
    assert "LIMIT" not in out.upper()


# ── Helpers ──────────────────────────────────────────────────────────

def test_sql_hash_is_stable_and_normalized():
    """Hash is whitespace-normalized + lowercased so trivial formatting
    differences produce identical cache keys."""
    h1 = ex._sql_hash("SELECT id FROM `case`")
    h2 = ex._sql_hash("select   ID\nfrom  `case`")
    assert h1 == h2, "Equivalent SQL must produce identical hash"
    assert len(h1) == 12


def test_is_analytical_query_detects_group_and_order_by():
    assert ex._is_analytical_query("SELECT status, COUNT(*) FROM `case` GROUP BY status")
    assert ex._is_analytical_query("SELECT id FROM `case` ORDER BY createdAt DESC")
    assert not ex._is_analytical_query("SELECT id FROM `case` WHERE status = 'OPEN'")


# ── _track_query_frequency + materialized cache ──────────────────────

def test_track_query_frequency_increments(tmp_path, monkeypatch):
    fake_path = tmp_path / "query_frequency.json"
    monkeypatch.setattr(ex, "_QUERY_FREQ_PATH", str(fake_path))

    assert ex._track_query_frequency("abc123") == 1
    assert ex._track_query_frequency("abc123") == 2
    assert ex._track_query_frequency("def456") == 1


def test_check_materialized_returns_none_when_file_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(ex, "_MATERIALIZED_DIR", str(tmp_path))
    assert ex._check_materialized("nonexistent_hash") is None


def test_check_materialized_returns_none_when_ttl_expired(tmp_path, monkeypatch):
    monkeypatch.setattr(ex, "_MATERIALIZED_DIR", str(tmp_path))
    monkeypatch.setattr(ex, "_MATERIALIZED_TTL", 0.001)
    path = tmp_path / "expired_hash.json"
    path.write_text(json.dumps([{"id": 1}]))
    time.sleep(0.01)
    assert ex._check_materialized("expired_hash") is None
    # The expired file should also be removed
    assert not path.exists()


def test_save_and_load_materialized_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(ex, "_MATERIALIZED_DIR", str(tmp_path))
    rows = [{"id": 1, "name": "x"}, {"id": 2, "name": "y"}]
    ex._save_materialized("test_hash", rows)
    loaded = ex._check_materialized("test_hash")
    assert loaded == rows


# ── Capability pin ───────────────────────────────────────────────────

def test_public_surface_preserved():
    """Capability pin — refactors must keep these names callable."""
    for name in (
        "execute_query",
        "execute_unlimited",
        "executor_node",
        "_bind_runtime_params",
        "_enforce_limit",
        "_sql_hash",
        "_is_analytical_query",
        "_track_query_frequency",
        "_check_materialized",
        "_save_materialized",
        "ROW_LIMIT",
        "QUERY_TIMEOUT_SECONDS",
        "DOWNLOAD_TIMEOUT_SECONDS",
    ):
        assert hasattr(ex, name), f"Public surface lost: {name}"
