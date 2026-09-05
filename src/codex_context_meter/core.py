"""Inspect bounded portions of an explicitly selected Codex rollout."""

from __future__ import annotations

import json
import math
import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO

DEFAULT_SCAN_BYTES = 8 * 1024 * 1024
MAX_SCAN_BYTES = 64 * 1024 * 1024
MAX_HISTORY_SEGMENTS = 128
UUID_PATTERN = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
USAGE_FIELDS = (
    "input_tokens", "cached_input_tokens", "cache_write_input_tokens",
    "output_tokens", "reasoning_output_tokens", "total_tokens",
)


class ContextMeterError(Exception):
    """A safe, structured error without conversation contents."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "status": "unavailable",
            "error": {"code": self.code, "message": str(self)},
        }


def _thread_id(value: str) -> str:
    if not isinstance(value, str) or not UUID_PATTERN.fullmatch(value):
        raise ContextMeterError("invalid_thread_id", "Provide the exact Codex thread UUID.")
    return value.lower()


def _integer(value: Any, *, positive: bool = False) -> bool:
    return type(value) is int and value >= (1 if positive else 0)


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_header(stream: BinaryIO, thread_id: str) -> dict[str, Any]:
    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
        raise ContextMeterError("invalid_session_file", "The rollout must be a regular file.")
    try:
        header = json.loads(stream.readline(1024 * 1024))
    except (ValueError, UnicodeDecodeError):
        raise ContextMeterError("invalid_session_header", "The rollout has no readable session metadata.") from None
    payload = header.get("payload") if isinstance(header, dict) else None
    if (
        not isinstance(header, dict)
        or header.get("type") != "session_meta"
        or not isinstance(payload, dict)
        or str(payload.get("id", "")).lower() != thread_id
    ):
        raise ContextMeterError("session_id_mismatch", "The rollout metadata does not match the requested thread.")
    return payload


def _resolve_session(home: Path, thread_id: str) -> list[tuple[Path, int | None]]:
    matches: dict[str, tuple[Path, dict[str, Any]]] = {}
    name_pattern = re.compile(
        rf"rollout-.+-{thread_id}(?:_({UUID_PATTERN.pattern}))?\.jsonl", re.IGNORECASE,
    )
    # Search only rollout filenames for the supplied UUID, never the newest session.
    for folder in ("sessions", "archived_sessions"):
        root = home / folder
        if not root.is_dir():
            continue
        if not root.resolve().is_relative_to(home):
            raise ContextMeterError("session_outside_home", "A rollout directory points outside CODEX_HOME.")
        for candidate in root.rglob(f"rollout-*-{thread_id}*.jsonl"):
            name_match = name_pattern.fullmatch(candidate.name)
            if name_match is None:
                continue
            resolved = candidate.resolve()
            if not resolved.is_relative_to(home):
                raise ContextMeterError("session_outside_home", "A matching rollout points outside CODEX_HOME.")
            if resolved.is_file():
                segment_id = (name_match.group(1) or thread_id).lower()
                if segment_id in matches:
                    if matches[segment_id][0] == resolved:
                        continue
                    raise ContextMeterError("ambiguous_session", "Multiple rollouts claim the same history segment.")
                if len(matches) >= MAX_HISTORY_SEGMENTS:
                    raise ContextMeterError("history_limit_exceeded", "Too many rollout segments; select an exact --session-file with the CLI.")
                with resolved.open("rb") as stream:
                    matches[segment_id] = (resolved, _read_header(stream, thread_id))
    if not matches:
        raise ContextMeterError(
            "session_not_found",
            "No uncompressed JSONL rollout matches this thread under CODEX_HOME. "
            "Check the thread UUID, local executor, and storage location.",
        )
    predecessors: dict[str, tuple[str, int]] = {}
    for segment_id, (_, metadata) in matches.items():
        # An unsuffixed rollout can itself be a fork of a different logical thread.
        # Only same-thread continuation segments participate in this chain.
        if segment_id == thread_id:
            continue
        base = metadata.get("history_base")
        if (
            not isinstance(base, dict)
            or not isinstance(base.get("thread_id"), str)
            or not UUID_PATTERN.fullmatch(base["thread_id"])
            or not _integer(base.get("end_byte_offset"))
        ):
            raise ContextMeterError("invalid_session_history", "A continuation has no valid history reference.")
        parent = base["thread_id"].lower()
        if parent not in matches:
            raise ContextMeterError("incomplete_session_history", "A referenced rollout segment is missing; select an exact --session-file with the CLI.")
        predecessors[segment_id] = (parent, base["end_byte_offset"])
    leaves = matches.keys() - {parent for parent, _ in predecessors.values()}
    if len(leaves) != 1:
        raise ContextMeterError(
            "ambiguous_session",
            "Rollout history branches or cycles; select an exact --session-file with the CLI.",
        )
    chain: list[tuple[Path, int | None]] = []
    visited: set[str] = set()
    segment_id = leaves.pop()
    end: int | None = None
    while segment_id not in visited:
        visited.add(segment_id)
        chain.append((matches[segment_id][0], end))
        if segment_id not in predecessors:
            break
        segment_id, end = predecessors[segment_id]
    if len(visited) != len(matches) or segment_id in predecessors:
        raise ContextMeterError("ambiguous_session", "Rollout history is disconnected or cyclic.")
    return list(reversed(chain))


def _read_records(
    path: Path, thread_id: str, max_scan_bytes: int, end_byte_offset: int | None = None,
) -> tuple[list[dict[str, Any]], bool, int]:
    try:
        with path.open("rb") as stream:
            _read_header(stream, thread_id)
            size = os.fstat(stream.fileno()).st_size
            if end_byte_offset is not None:
                if end_byte_offset > size:
                    raise ContextMeterError("invalid_session_history", "An inherited history boundary exceeds the rollout size.")
                size = end_byte_offset
                if size:
                    stream.seek(size - 1)
                    if stream.read(1) != b"\n":
                        raise ContextMeterError("invalid_session_history", "An inherited history boundary splits a record.")
            start = max(0, size - max_scan_bytes)
            stream.seek(max(0, start - 1))
            before = stream.read(1) if start else b"\n"
            stream.seek(start)
            data = stream.read(size - start)
    except ContextMeterError:
        raise
    except OSError:
        raise ContextMeterError("session_unreadable", "The selected rollout could not be read.") from None

    if start and before != b"\n":
        _, separator, data = data.partition(b"\n")
        if not separator:
            data = b""
    # Writers may be appending a record. Only newline-terminated records are stable.
    incomplete_tail = bool(data and not data.endswith(b"\n"))
    lines = data.split(b"\n")[:-1]
    records: list[dict[str, Any]] = []
    for raw in lines:
        try:
            record = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(record, dict):
            records.append(record)
    return records, incomplete_tail, size - start


def _read_history(
    chain: list[tuple[Path, int | None]], thread_id: str, max_scan_bytes: int,
) -> tuple[list[dict[str, Any]], bool]:
    chunks = []
    incomplete_tail = False
    remaining = max_scan_bytes
    for path, end in reversed(chain):
        records, incomplete, scanned = _read_records(path, thread_id, remaining, end)
        chunks.append(records)
        incomplete_tail |= incomplete
        remaining -= scanned
        if remaining <= 0:
            break
    return [record for chunk in reversed(chunks) for record in chunk], incomplete_tail


def read_context_usage(
    thread_id: str,
    *,
    codex_home: str | Path | None = None,
    session_file: str | Path | None = None,
    stale_after_seconds: float = 120,
    auto_compact_token_limit: int | None = None,
    max_scan_bytes: int = DEFAULT_SCAN_BYTES,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return a usage snapshot, never cumulative cost or exact live memory usage.

    An explicit session_file is a CLI/library escape hatch for nonstandard storage.
    The MCP surface intentionally does not expose arbitrary filesystem paths.
    """
    thread_id = _thread_id(thread_id)
    if (
        isinstance(stale_after_seconds, bool)
        or not isinstance(stale_after_seconds, (int, float))
        or not math.isfinite(stale_after_seconds)
        or stale_after_seconds < 0
    ):
        raise ContextMeterError("invalid_stale_after", "stale_after_seconds must be a finite nonnegative number.")
    if not _integer(max_scan_bytes, positive=True) or max_scan_bytes > MAX_SCAN_BYTES:
        raise ContextMeterError("invalid_scan_limit", "max_scan_bytes must be between 1 and 67108864.")
    if auto_compact_token_limit is not None and not _integer(auto_compact_token_limit, positive=True):
        raise ContextMeterError("invalid_compact_limit", "Provide a positive explicit compaction threshold or omit it.")
    observed = now or datetime.now(timezone.utc)
    if observed.tzinfo is None:
        raise ContextMeterError("invalid_time", "The observation time must include a timezone.")
    home = Path(codex_home or os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser().resolve()
    try:
        chain = (
            [(Path(session_file).expanduser().resolve(), None)]
            if session_file else _resolve_session(home, thread_id)
        )
    except (OSError, RuntimeError):
        raise ContextMeterError("session_unreadable", "The rollout location could not be inspected.") from None
    records, incomplete_tail = _read_history(chain, thread_id, max_scan_bytes)
    latest: dict[str, Any] | None = None
    latest_index = -1
    compacted_index = -1
    awaiting_usage = False
    previous_signature = None
    for index, record in enumerate(records):
        payload = record.get("payload")
        is_compaction = record.get("type") == "compacted" or (
            record.get("type") == "event_msg"
            and isinstance(payload, dict)
            and payload.get("type") == "context_compacted"
        )
        if is_compaction:
            awaiting_usage = True
            compacted_index = index
        if not isinstance(payload, dict):
            continue
        if record.get("type") == "event_msg" and payload.get("type") == "token_count" and payload.get("info") is not None:
            info = payload["info"]
            signature = (
                {key: info.get(key) for key in ("last_token_usage", "total_token_usage", "model_context_window")}
                if isinstance(info, dict) else info
            )
            # Newer events can replay old counters, including after compaction.
            if awaiting_usage and previous_signature is not None and signature != previous_signature:
                awaiting_usage = False
            previous_signature = signature
            latest = record
            latest_index = index
    if latest is None:
        raise ContextMeterError(
            "usage_not_found",
            "No complete token_count usage record was found in the bounded tail. "
            "Wait for a new model response or increase the scan limit with the CLI.",
        )
    info = latest["payload"]["info"]
    compacted_after = compacted_index > latest_index
    usage = info.get("last_token_usage") if isinstance(info, dict) else None
    if not isinstance(usage, dict) or any(not _integer(usage.get(k)) for k in ("input_tokens", "total_tokens")):
        raise ContextMeterError("invalid_usage", "The latest record has no valid last-request usage counters.")
    if usage["total_tokens"] < usage["input_tokens"]:
        raise ContextMeterError("invalid_usage", "The latest request counters are inconsistent.")
    for field in USAGE_FIELDS:
        if field in usage and not _integer(usage[field]):
            raise ContextMeterError("invalid_usage", "A last-request usage counter is not a nonnegative integer.")
    last_request = {key: usage[key] for key in USAGE_FIELDS if key in usage}
    window = info.get("model_context_window")
    window = window if _integer(window, positive=True) else None
    event_time = _timestamp(latest.get("timestamp"))
    age = (observed - event_time).total_seconds() if event_time else None
    warnings = ["last_recorded_request_not_live_usage", "event_time_may_repeat_older_counters"]
    if age is None:
        warnings.append("event_timestamp_unknown")
    elif age < 0:
        warnings.append("event_timestamp_in_future")
    if incomplete_tail:
        warnings.append("incomplete_tail_record_ignored")
    stale = age is not None and age > stale_after_seconds
    status = "stale" if stale else "ok"
    remaining = None
    if window is None:
        status = "window_unknown"
    elif not awaiting_usage:
        used = usage["total_tokens"]
        remaining = {
            "tokens": max(window - used, 0),
            "percent": round(max(window - used, 0) / window * 100, 1),
            "over_window_tokens": max(used - window, 0),
            "basis": "window_tokens - last_request.total_tokens",
        }
    if awaiting_usage:
        status = "awaiting_usage_after_compaction"
        warnings.append("compaction_after_latest_usage" if compacted_after else "usage_not_changed_since_compaction")
    return {
        "schema_version": 1,
        "thread_id": thread_id,
        "source": "codex_rollout_token_count",
        "status": status,
        "observed_at": _iso(observed),
        "event_at": _iso(event_time) if event_time else None,
        "event_age_seconds": round(age, 3) if age is not None else None,
        "measurement_time_known": False,
        "is_stale": stale if age is not None and age >= 0 else None,
        "stale_after_seconds": stale_after_seconds,
        "window_tokens": window,
        "last_request": last_request,
        "remaining_estimate": remaining,
        "compaction": {
            "threshold_tokens": auto_compact_token_limit,
            "threshold_source": "caller" if auto_compact_token_limit is not None else "unknown",
            "remaining_estimate_tokens": (
                max(auto_compact_token_limit - usage["total_tokens"], 0)
                if auto_compact_token_limit is not None and not awaiting_usage else None
            ),
            "after_latest_usage": compacted_after,
            "awaiting_new_usage": awaiting_usage,
        },
        "warnings": warnings,
    }
