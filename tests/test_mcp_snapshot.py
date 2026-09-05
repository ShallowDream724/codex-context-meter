"""Compact planning responses preserve uncertainty and withhold invalid budgets."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from codex_context_meter.core import read_context_usage
from codex_context_meter.server import _compact_snapshot

THREAD_ID = "11111111-1111-1111-1111-111111111111"
NOW = datetime(2026, 1, 2, 12, tzinfo=timezone.utc)


def snapshot(tmp_path, *, window=1000, used=400, age=1.75, compacted=False, **kwargs):
    timestamp = (NOW - timedelta(seconds=age)).isoformat() if age is not None else "invalid"
    event = {
        "type": "event_msg", "timestamp": timestamp,
        "payload": {"type": "token_count", "info": {
            "model_context_window": window,
            "last_token_usage": {"input_tokens": used - 10, "total_tokens": used},
            "total_token_usage": {"total_tokens": 99_999_999},
        }},
    }
    records = [{"type": "session_meta", "payload": {"id": THREAD_ID}}, event]
    if compacted:
        records.extend([{"type": "compacted", "payload": {}}, event])
    path = tmp_path / "rollout.jsonl"
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    return read_context_usage(THREAD_ID, session_file=path, now=NOW, **kwargs)


def test_compact_budget_omits_diagnostics_and_cumulative_usage(tmp_path):
    detailed = snapshot(tmp_path)
    before = json.dumps(detailed)

    assert _compact_snapshot(detailed) == {
        "status": "ok", "used_tokens": 400, "window_tokens": 1000,
        "remaining_tokens": 600, "event_age_seconds": 1,
    }
    assert json.dumps(detailed) == before


def test_stale_snapshot_keeps_its_age_and_historical_budget(tmp_path):
    result = _compact_snapshot(snapshot(tmp_path, age=121.25))
    assert result["status"] == "stale"
    assert result["event_age_seconds"] == 121
    assert result["remaining_tokens"] == 600


@pytest.mark.parametrize("age", [None, -60, -0.0001])
def test_unknown_or_future_timestamp_cannot_appear_fresh(tmp_path, age):
    result = _compact_snapshot(snapshot(tmp_path, age=age))
    assert result["status"] == "event_time_unknown"
    assert result["event_age_seconds"] is None


@pytest.mark.parametrize("age", [1, None, -60])
def test_compaction_withholds_used_remaining_and_threshold_budgets(tmp_path, age):
    result = _compact_snapshot(snapshot(tmp_path, compacted=True, age=age, auto_compact_token_limit=800))
    assert result["status"] == "awaiting_usage_after_compaction"
    assert result["used_tokens"] is None
    assert result["remaining_tokens"] is None
    assert result["compaction_remaining_tokens"] is None


@pytest.mark.parametrize("age", [1, None, -60])
def test_unknown_window_does_not_invent_remaining_tokens(tmp_path, age):
    result = _compact_snapshot(snapshot(tmp_path, window=None, age=age))
    assert result["status"] == "window_unknown"
    assert result["used_tokens"] == 400
    assert result["window_tokens"] is None
    assert result["remaining_tokens"] is None


def test_explicit_threshold_adds_only_its_remaining_budget(tmp_path):
    result = _compact_snapshot(snapshot(tmp_path, auto_compact_token_limit=800))
    assert result["compaction_remaining_tokens"] == 400


def test_over_window_counters_are_visible_with_zero_remaining(tmp_path):
    result = _compact_snapshot(snapshot(tmp_path, used=1200, auto_compact_token_limit=900))
    assert result["used_tokens"] == 1200
    assert result["remaining_tokens"] == 0
    assert result["compaction_remaining_tokens"] == 0
