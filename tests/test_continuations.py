"""Synthetic paginated rollouts preserve identity, history boundaries, and scan limits."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from codex_context_meter.core import ContextMeterError, read_context_usage

THREAD = "11111111-1111-1111-1111-111111111111"
SEGMENTS = [f"22222222-2222-2222-2222-{index:012d}" for index in range(4)]
NOW = datetime(2026, 1, 5, 12, tzinfo=timezone.utc)
COMPACTION = {"type": "compacted", "payload": {}}


def usage(total: int, *, old: bool = False) -> dict:
    return {
        "timestamp": (NOW - timedelta(days=3) if old else NOW).isoformat(),
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "last_token_usage": {"input_tokens": total - 10, "total_tokens": total},
                "total_token_usage": {"total_tokens": total * 100},
                "model_context_window": 1000,
            },
        },
    }


def append(path: Path, *records: dict) -> None:
    with path.open("ab") as stream:
        for record in records:
            stream.write((json.dumps(record) + "\n").encode())


def rollout(home: Path, segment: str, *records: dict, parent=None, offset=None, folder="sessions") -> Path:
    directory = home / folder
    directory.mkdir(exist_ok=True)
    suffix = "" if segment == THREAD else f"_{segment}"
    path = directory / f"rollout-synthetic-{THREAD}{suffix}.jsonl"
    metadata = {"id": THREAD, "session_id": THREAD, "history_mode": "paginated"}
    if parent is not None:
        metadata["history_base"] = {
            "thread_id": parent, "end_ordinal_exclusive": 100, "end_byte_offset": offset,
        }
    append(path, {"type": "session_meta", "payload": metadata}, *records)
    return path


def read(home: Path, **kwargs) -> dict:
    return read_context_usage(THREAD, codex_home=home, now=NOW, **kwargs)


def error(home: Path, code: str, **kwargs) -> None:
    with pytest.raises(ContextMeterError) as caught:
        read(home, **kwargs)
    assert caught.value.code == code
    assert str(home) not in json.dumps(caught.value.as_dict())


def test_three_resumes_select_terminal_segment_even_when_original_mtime_is_newer(tmp_path):
    root = rollout(tmp_path, THREAD, usage(420, old=True))
    previous, previous_id = root, THREAD
    for segment, total in zip(SEGMENTS, (480, 520, 600)):
        previous = rollout(tmp_path, segment, usage(total), parent=previous_id, offset=previous.stat().st_size)
        previous_id = segment
    os.utime(root, (NOW.timestamp() + 1000, NOW.timestamp() + 1000))

    result = read(tmp_path)

    assert result["status"] == "ok"
    assert result["last_request"]["total_tokens"] == 600
    assert result["remaining_estimate"]["percent"] == 40.0
    assert result["event_age_seconds"] == 0


def test_empty_continuation_uses_inherited_snapshot_without_refreshing_its_time(tmp_path):
    root = rollout(tmp_path, THREAD, usage(420, old=True), folder="archived_sessions")
    rollout(tmp_path, SEGMENTS[0], parent=THREAD, offset=root.stat().st_size)

    result = read(tmp_path)

    assert result["status"] == "stale"
    assert result["event_age_seconds"] == 3 * 86400
    assert result["last_request"]["total_tokens"] == 420


def test_predecessor_records_beyond_inherited_boundary_do_not_affect_snapshot(tmp_path):
    root = rollout(tmp_path, THREAD, usage(420))
    boundary = root.stat().st_size
    append(root, COMPACTION, usage(200))
    rollout(tmp_path, SEGMENTS[0], parent=THREAD, offset=boundary)

    result = read(tmp_path)

    assert result["status"] == "ok"
    assert result["last_request"]["total_tokens"] == 420


@pytest.mark.parametrize("compaction_in_child", [False, True])
def test_replayed_counters_do_not_clear_compaction_across_segment_boundary(tmp_path, compaction_in_child):
    root = rollout(tmp_path, THREAD, usage(420))
    if not compaction_in_child:
        append(root, COMPACTION)
    child_records = [COMPACTION, usage(420)] if compaction_in_child else [usage(420)]
    child = rollout(tmp_path, SEGMENTS[0], *child_records, parent=THREAD, offset=root.stat().st_size)

    result = read(tmp_path)
    assert result["status"] == "awaiting_usage_after_compaction"
    assert result["remaining_estimate"] is None

    append(child, usage(180))
    assert read(tmp_path)["remaining_estimate"]["tokens"] == 820


def test_compaction_only_continuation_invalidates_inherited_snapshot(tmp_path):
    root = rollout(tmp_path, THREAD, usage(420))
    rollout(tmp_path, SEGMENTS[0], COMPACTION, parent=THREAD, offset=root.stat().st_size)

    result = read(tmp_path)

    assert result["status"] == "awaiting_usage_after_compaction"
    assert result["compaction"]["after_latest_usage"] is True


def test_total_tail_budget_is_shared_across_segments(tmp_path):
    root = rollout(tmp_path, THREAD, usage(420))
    child = rollout(tmp_path, SEGMENTS[0], parent=THREAD, offset=root.stat().st_size)
    error(tmp_path, "usage_not_found", max_scan_bytes=child.stat().st_size)
    assert read(tmp_path)["last_request"]["total_tokens"] == 420


def test_branching_history_is_ambiguous_regardless_of_timestamps(tmp_path):
    root = rollout(tmp_path, THREAD, usage(420))
    for segment in SEGMENTS[:2]:
        rollout(tmp_path, segment, usage(600), parent=THREAD, offset=root.stat().st_size)
    error(tmp_path, "ambiguous_session")


@pytest.mark.parametrize("shape", ["self", "cycle", "disconnected_cycle"])
def test_cyclic_history_is_rejected(tmp_path, shape):
    if shape == "disconnected_cycle":
        rollout(tmp_path, THREAD, usage(420))
    rollout(tmp_path, SEGMENTS[0], usage(600), parent=SEGMENTS[0] if shape == "self" else SEGMENTS[1], offset=0)
    if shape != "self":
        rollout(tmp_path, SEGMENTS[1], parent=SEGMENTS[0], offset=0)
    error(tmp_path, "ambiguous_session")


def test_missing_predecessor_does_not_fall_back_to_original(tmp_path):
    rollout(tmp_path, THREAD, usage(420, old=True))
    rollout(tmp_path, SEGMENTS[0], usage(600), parent=SEGMENTS[1], offset=100)
    error(tmp_path, "incomplete_session_history")


@pytest.mark.parametrize("offset", [True, -1, 1.5, "100", None, 1, 1_000_000])
def test_invalid_or_unreadable_history_boundaries_are_rejected(tmp_path, offset):
    rollout(tmp_path, THREAD, usage(420))
    rollout(tmp_path, SEGMENTS[0], usage(600), parent=THREAD, offset=offset)
    error(tmp_path, "invalid_session_history")


def test_zero_history_boundary_does_not_import_predecessor_usage(tmp_path):
    rollout(tmp_path, THREAD, usage(420))
    rollout(tmp_path, SEGMENTS[0], parent=THREAD, offset=0)
    error(tmp_path, "usage_not_found")


def test_continuation_without_history_reference_is_rejected(tmp_path):
    rollout(tmp_path, THREAD, usage(420))
    rollout(tmp_path, SEGMENTS[0], usage(600))
    error(tmp_path, "invalid_session_history")


def test_metadata_identity_is_checked_for_every_discovered_segment(tmp_path):
    root = rollout(tmp_path, THREAD, usage(420))
    child = rollout(tmp_path, SEGMENTS[0], usage(600), parent=THREAD, offset=root.stat().st_size)
    lines = child.read_text().splitlines()
    header = json.loads(lines[0])
    header["payload"]["id"] = SEGMENTS[0]
    child.write_text(json.dumps(header) + "\n" + "\n".join(lines[1:]) + "\n", encoding="utf-8")
    error(tmp_path, "session_id_mismatch")


def test_explicit_file_remains_an_exact_selection(tmp_path):
    root = rollout(tmp_path, THREAD, usage(420, old=True))
    child = rollout(tmp_path, SEGMENTS[0], usage(600), parent=THREAD, offset=root.stat().st_size)

    assert read(tmp_path, session_file=root)["last_request"]["total_tokens"] == 420
    assert read(tmp_path, session_file=child)["last_request"]["total_tokens"] == 600


def test_duplicate_continuation_id_in_archive_is_ambiguous(tmp_path):
    root = rollout(tmp_path, THREAD, usage(420))
    for folder in ("sessions", "archived_sessions"):
        rollout(tmp_path, SEGMENTS[0], usage(600), parent=THREAD, offset=root.stat().st_size, folder=folder)
    error(tmp_path, "ambiguous_session")


def test_unrelated_suffix_text_is_not_treated_as_a_continuation(tmp_path):
    root = rollout(tmp_path, THREAD, usage(420))
    (root.parent / f"rollout-synthetic-{THREAD}_backup.jsonl").write_text("private", encoding="utf-8")
    assert read(tmp_path)["last_request"]["total_tokens"] == 420


def test_discovery_limits_total_segment_headers(tmp_path, monkeypatch):
    monkeypatch.setattr("codex_context_meter.core.MAX_HISTORY_SEGMENTS", 2)
    previous = rollout(tmp_path, THREAD, usage(420))
    parent = THREAD
    for segment in SEGMENTS[:2]:
        previous = rollout(tmp_path, segment, usage(600), parent=parent, offset=previous.stat().st_size)
        parent = segment
    error(tmp_path, "history_limit_exceeded")


def test_continuation_symlink_cannot_escape_codex_home(tmp_path):
    home = tmp_path / "home"
    outside = tmp_path / "outside"
    home.mkdir()
    outside.mkdir()
    root = rollout(home, THREAD, usage(420))
    target = rollout(outside, SEGMENTS[0], usage(600), parent=THREAD, offset=root.stat().st_size)
    link = root.parent / target.name
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("This environment cannot create symlinks.")
    error(home, "session_outside_home")


def test_cyclic_symlink_produces_a_structured_error(tmp_path):
    folder = tmp_path / "sessions"
    folder.mkdir()
    link = folder / f"rollout-synthetic-{THREAD}.jsonl"
    try:
        link.symlink_to(link)
    except (OSError, NotImplementedError):
        pytest.skip("This environment cannot create symlinks.")
    error(tmp_path, "session_unreadable")
