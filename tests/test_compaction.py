import json
from datetime import datetime, timezone

from codex_context_meter import read_context_usage

THREAD_ID = "00000000-0000-0000-0000-000000000008"
NOW = datetime(2026, 1, 1, 0, 10, tzinfo=timezone.utc)


def usage(total, timestamp="2026-01-01T00:09:00Z", cumulative=1000):
    return {
        "type": "event_msg",
        "timestamp": timestamp,
        "payload": {
            "type": "token_count",
            "info": {
                "last_token_usage": {"input_tokens": total - 10, "total_tokens": total},
                "total_token_usage": {"total_tokens": cumulative},
                "model_context_window": 1000,
            },
        },
    }


def read(tmp_path, records):
    file = tmp_path / "session.jsonl"
    rows = [{"type": "session_meta", "payload": {"id": THREAD_ID}}, *records]
    file.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return read_context_usage(THREAD_ID, session_file=file, now=NOW)


def test_repeated_counters_after_compaction_do_not_restore_estimate(tmp_path):
    result = read(tmp_path, [usage(800), {"type": "compacted"}, usage(800, "2026-01-01T00:09:59Z")])
    assert result["status"] == "awaiting_usage_after_compaction"
    assert result["remaining_estimate"] is None
    assert result["compaction"]["after_latest_usage"] is False
    assert result["compaction"]["awaiting_new_usage"] is True


def test_changed_usage_after_compaction_restores_estimate(tmp_path):
    result = read(tmp_path, [usage(800), {"type": "compacted"}, usage(100, cumulative=1100)])
    assert result["status"] == "ok"
    assert result["remaining_estimate"]["tokens"] == 900
    assert result["compaction"]["awaiting_new_usage"] is False


def test_absent_pre_compaction_baseline_is_conservative(tmp_path):
    result = read(tmp_path, [{"type": "compacted"}, usage(100)])
    assert result["status"] == "awaiting_usage_after_compaction"
    assert result["remaining_estimate"] is None


def test_cumulative_usage_can_establish_a_new_request(tmp_path):
    result = read(tmp_path, [usage(100), {"type": "compacted"}, usage(100, cumulative=1200)])
    assert result["status"] == "ok"
    assert result["remaining_estimate"]["tokens"] == 900
