"""Behavioral tests for bounded, read-only rollout snapshots."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from codex_context_meter.core import ContextMeterError, read_context_usage


THREAD_ID = "11111111-1111-1111-1111-111111111111"
OTHER_THREAD_ID = "22222222-2222-2222-2222-222222222222"
NOW = datetime(2026, 1, 2, 12, 0, tzinfo=timezone.utc)


def token_count(
    *,
    usage: dict | None = None,
    window: object = 1_000,
    timestamp: object | None = None,
) -> dict:
    info: dict = {"last_token_usage": usage or {"input_tokens": 100, "total_tokens": 250}}
    if window is not ...:
        info["model_context_window"] = window
    return {
        "type": "event_msg",
        "timestamp": timestamp if timestamp is not None else NOW.isoformat(),
        "payload": {"type": "token_count", "info": info},
    }


def write_rollout(tmp_path: Path, records: list[dict], *, filename: str | None = None) -> Path:
    path = tmp_path / (filename or f"rollout-synthetic-{THREAD_ID}.jsonl")
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    return path


def valid_records(*records: dict) -> list[dict]:
    return [{"type": "session_meta", "payload": {"id": THREAD_ID}}, *records]


def read(path: Path, **kwargs) -> dict:
    return read_context_usage(THREAD_ID, session_file=path, now=NOW, **kwargs)


def error_code(path: Path, **kwargs) -> str:
    with pytest.raises(ContextMeterError) as exc:
        read(path, **kwargs)
    return exc.value.code


def test_snapshot_uses_only_latest_last_request_for_window_math(tmp_path):
    latest = token_count(usage={
        "input_tokens": 300, "cached_input_tokens": 200, "output_tokens": 100,
        "reasoning_output_tokens": 90, "total_tokens": 400,
    })
    latest["payload"]["info"]["total_token_usage"] = {"total_tokens": 9_000_000}
    path = write_rollout(
        tmp_path,
        valid_records(
            token_count(usage={"input_tokens": 100, "output_tokens": 40, "cached_input_tokens": 70, "total_tokens": 140}),
            latest,
        ),
    )

    result = read(path, auto_compact_token_limit=500)

    assert result["status"] == "ok"
    assert result["last_request"] == {
        "input_tokens": 300,
        "cached_input_tokens": 200,
        "output_tokens": 100,
        "reasoning_output_tokens": 90,
        "total_tokens": 400,
    }
    assert result["remaining_estimate"] == {
        "tokens": 600,
        "percent": 60.0,
        "over_window_tokens": 0,
        "basis": "window_tokens - last_request.total_tokens",
    }
    assert result["compaction"]["remaining_estimate_tokens"] == 100
    assert result["measurement_time_known"] is False
    assert "last_recorded_request_not_live_usage" in result["warnings"]


def test_read_is_nonmutating_and_does_not_return_conversation_contents(tmp_path):
    marker = "synthetic-private-content-sentinel"
    latest = token_count()
    latest["payload"]["info"]["extra"] = marker
    path = write_rollout(tmp_path, valid_records({"type": "response_item", "payload": {"text": marker}}, latest))
    before = path.read_bytes()
    modified = path.stat().st_mtime_ns

    result = read(path)

    assert marker not in json.dumps(result)
    assert str(path) not in json.dumps(result)
    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == modified


def test_discovery_binds_requested_id_in_archive_and_ignores_other_threads(tmp_path):
    archive = tmp_path / "archived_sessions"
    sessions = tmp_path / "sessions"
    archive.mkdir()
    sessions.mkdir()
    write_rollout(archive, valid_records(token_count()))
    write_rollout(sessions, [{"type": "session_meta", "payload": {"id": OTHER_THREAD_ID}}, token_count()], filename=f"rollout-newer-{OTHER_THREAD_ID}.jsonl")

    result = read_context_usage(THREAD_ID, codex_home=tmp_path, now=NOW)

    assert result["thread_id"] == THREAD_ID
    assert result["remaining_estimate"]["tokens"] == 750


def test_discovery_does_not_fall_back_to_an_unrelated_thread(tmp_path):
    folder = tmp_path / "sessions"
    folder.mkdir()
    write_rollout(folder, [{"type": "session_meta", "payload": {"id": OTHER_THREAD_ID}}, token_count()], filename=f"rollout-newer-{OTHER_THREAD_ID}.jsonl")
    with pytest.raises(ContextMeterError) as exc:
        read_context_usage(THREAD_ID, codex_home=tmp_path, now=NOW)
    assert exc.value.code == "session_not_found"


def test_discovery_rejects_a_symlink_outside_codex_home(tmp_path):
    home = tmp_path / "home"
    folder = home / "sessions"
    folder.mkdir(parents=True)
    target = write_rollout(tmp_path, valid_records(token_count()))
    link = folder / target.name
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("This environment cannot create symlinks.")
    with pytest.raises(ContextMeterError) as exc:
        read_context_usage(THREAD_ID, codex_home=home, now=NOW)
    assert exc.value.code == "session_outside_home"


@pytest.mark.parametrize("window", [None, ..., 0, -10, True, float("nan")])
def test_missing_or_invalid_window_is_reported_without_inventing_remaining(tmp_path, window):
    path = write_rollout(tmp_path, valid_records(token_count(window=window)))

    result = read(path)

    assert result["status"] == "window_unknown"
    assert result["window_tokens"] is None
    assert result["remaining_estimate"] is None
    assert result["last_request"]["total_tokens"] == 250


def test_stale_future_and_unknown_event_times_are_explicit(tmp_path):
    stale_dir = tmp_path / "stale"
    future_dir = tmp_path / "future"
    unknown_dir = tmp_path / "unknown"
    stale_dir.mkdir()
    future_dir.mkdir()
    unknown_dir.mkdir()
    stale = write_rollout(stale_dir, valid_records(token_count(timestamp=(NOW - timedelta(seconds=121)).isoformat())))
    future = write_rollout(future_dir, valid_records(token_count(timestamp=(NOW + timedelta(seconds=1)).isoformat())))
    unknown = write_rollout(unknown_dir, valid_records(token_count(timestamp="not-a-time")))

    stale_result = read(stale)
    future_result = read(future)
    unknown_result = read(unknown)

    assert stale_result["status"] == "stale"
    assert stale_result["is_stale"] is True
    assert stale_result["event_age_seconds"] == 121.0
    assert future_result["is_stale"] is None
    assert "event_timestamp_in_future" in future_result["warnings"]
    assert unknown_result["event_at"] is None
    assert unknown_result["is_stale"] is None
    assert "event_timestamp_unknown" in unknown_result["warnings"]


def test_missing_usage_and_missing_file_return_safe_errors(tmp_path):
    no_usage = write_rollout(tmp_path, valid_records({"type": "event_msg", "payload": {"type": "other"}}))

    assert error_code(no_usage) == "usage_not_found"
    assert error_code(tmp_path / "missing.jsonl") == "session_unreadable"


@pytest.mark.parametrize("candidate", ["../outside", "not-a-uuid", "11111111-1111-1111-1111-111111111111.jsonl"])
def test_thread_identifier_cannot_inject_paths(candidate, tmp_path):
    with pytest.raises(ContextMeterError) as exc:
        read_context_usage(candidate, session_file=tmp_path / "unused.jsonl", now=NOW)
    assert exc.value.code == "invalid_thread_id"


def test_discovery_rejects_multiple_matching_rollouts(tmp_path):
    for folder in (tmp_path / "sessions" / "a", tmp_path / "archived_sessions" / "b"):
        folder.mkdir(parents=True)
        write_rollout(folder, valid_records(token_count()), filename=f"rollout-one-{THREAD_ID}.jsonl")

    with pytest.raises(ContextMeterError) as exc:
        read_context_usage(THREAD_ID, codex_home=tmp_path, now=NOW)
    assert exc.value.code == "ambiguous_session"


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ({"type": "session_meta", "payload": {"id": OTHER_THREAD_ID}}, "session_id_mismatch"),
        (["not", "an", "object"], "session_id_mismatch"),
        ({"type": "event_msg", "payload": {"id": THREAD_ID}}, "session_id_mismatch"),
    ],
)
def test_session_metadata_binds_file_to_requested_thread(tmp_path, header, expected):
    path = tmp_path / "rollout-synthetic.jsonl"
    path.write_text(json.dumps(header) + "\n" + json.dumps(token_count()) + "\n", encoding="utf-8")

    assert error_code(path) == expected


def test_partial_tail_is_ignored_and_bounded_tail_starts_on_a_record_boundary(tmp_path):
    latest = token_count(usage={"input_tokens": 50, "total_tokens": 125})
    path = write_rollout(tmp_path, valid_records({"type": "note", "payload": {"padding": "x" * 4_000}}, latest))
    with path.open("ab") as stream:
        stream.write(b'{"type":"event_msg","payload":')

    result = read(path, max_scan_bytes=len(json.dumps(latest)) + 32)

    assert result["last_request"]["total_tokens"] == 125
    assert "incomplete_tail_record_ignored" in result["warnings"]


@pytest.mark.parametrize(
    "marker",
    [
        {"type": "compacted", "payload": {}},
        {"type": "event_msg", "payload": {"type": "context_compacted"}},
    ],
)
def test_compaction_after_usage_invalidates_the_previous_remaining_estimate(tmp_path, marker):
    path = write_rollout(tmp_path, valid_records(token_count(), marker))

    result = read(path, auto_compact_token_limit=300)

    assert result["status"] == "awaiting_usage_after_compaction"
    assert result["remaining_estimate"] is None
    assert result["compaction"]["after_latest_usage"] is True
    assert result["compaction"]["remaining_estimate_tokens"] is None


def test_usage_after_compaction_restores_a_fresh_snapshot(tmp_path):
    path = write_rollout(
        tmp_path,
        valid_records(token_count(usage={"input_tokens": 100, "total_tokens": 250}), {"type": "compacted", "payload": {}}, token_count(usage={"input_tokens": 30, "total_tokens": 80})),
    )

    result = read(path)

    assert result["status"] == "ok"
    assert result["remaining_estimate"]["tokens"] == 920
    assert result["compaction"]["after_latest_usage"] is False


@pytest.mark.parametrize(
    "usage",
    [
        {"input_tokens": -1, "total_tokens": 1},
        {"input_tokens": True, "total_tokens": 1},
        {"input_tokens": 1, "total_tokens": float("nan")},
        {"input_tokens": 3, "total_tokens": 2},
        {"input_tokens": 1, "output_tokens": -1, "total_tokens": 2},
        {"input_tokens": 1},
    ],
)
def test_invalid_last_request_counters_are_rejected(tmp_path, usage):
    path = write_rollout(tmp_path, valid_records(token_count(usage=usage)))

    assert error_code(path) == "invalid_usage"


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"stale_after_seconds": -1}, "invalid_stale_after"),
        ({"stale_after_seconds": True}, "invalid_stale_after"),
        ({"stale_after_seconds": float("nan")}, "invalid_stale_after"),
        ({"max_scan_bytes": 0}, "invalid_scan_limit"),
        ({"max_scan_bytes": True}, "invalid_scan_limit"),
        ({"max_scan_bytes": 64 * 1024 * 1024 + 1}, "invalid_scan_limit"),
        ({"auto_compact_token_limit": 0}, "invalid_compact_limit"),
        ({"auto_compact_token_limit": True}, "invalid_compact_limit"),
        ({"auto_compact_token_limit": 12.5}, "invalid_compact_limit"),
    ],
)
def test_invalid_parameters_have_structured_errors(tmp_path, kwargs, expected):
    path = write_rollout(tmp_path, valid_records(token_count()))

    assert error_code(path, **kwargs) == expected


def test_naive_observation_time_and_error_serialization_are_safe(tmp_path):
    path = write_rollout(tmp_path, valid_records(token_count()))
    with pytest.raises(ContextMeterError) as exc:
        read_context_usage(THREAD_ID, session_file=path, now=datetime(2026, 1, 2))

    assert exc.value.code == "invalid_time"
    assert exc.value.as_dict() == {
        "schema_version": 1,
        "status": "unavailable",
        "error": {"code": "invalid_time", "message": "The observation time must include a timezone."},
    }
