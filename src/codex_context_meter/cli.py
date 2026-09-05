"""JSON command-line interface."""

from __future__ import annotations

import argparse
import json
import os

from . import __version__
from .core import DEFAULT_SCAN_BYTES, ContextMeterError, read_context_usage


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read the latest recorded context usage of an explicit Codex thread.")
    parser.add_argument("--thread-id", help="Thread UUID; defaults to CODEX_THREAD_ID only when that environment variable is present.")
    parser.add_argument("--codex-home", help="Codex data directory; defaults to CODEX_HOME or ~/.codex.")
    parser.add_argument("--session-file", help="Exact uncompressed rollout JSONL; its session metadata must match the thread UUID.")
    parser.add_argument("--compact-threshold", type=int, help="Known total-context compaction threshold; never inferred from window size.")
    parser.add_argument("--stale-after", type=float, default=120, help="Flag log events older than this many seconds (default: 120).")
    parser.add_argument("--max-scan-bytes", type=int, default=DEFAULT_SCAN_BYTES, help="Maximum tail bytes to inspect (default: 8 MiB; maximum: 64 MiB).")
    parser.add_argument("--pretty", action="store_true", help="Indent the JSON response.")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)
    try:
        thread_id = args.thread_id if args.thread_id is not None else os.environ.get("CODEX_THREAD_ID")
        if not thread_id:
            raise ContextMeterError("thread_id_required", "Pass --thread-id or run in an executor that supplies CODEX_THREAD_ID.")
        result = read_context_usage(
            thread_id,
            codex_home=args.codex_home,
            session_file=args.session_file,
            stale_after_seconds=args.stale_after,
            auto_compact_token_limit=args.compact_threshold,
            max_scan_bytes=args.max_scan_bytes,
        )
        exit_code = 0
    except ContextMeterError as exc:
        result, exit_code = exc.as_dict(), 2
    print(json.dumps(result, indent=2 if args.pretty else None, ensure_ascii=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
