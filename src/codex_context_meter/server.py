"""Optional MCP stdio interface using the official Python SDK."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .core import ContextMeterError, read_context_usage


def _compact_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    awaiting_usage = snapshot["compaction"]["awaiting_new_usage"]
    age = snapshot["event_age_seconds"]
    valid_age = age is not None and snapshot["is_stale"] is not None
    status = snapshot["status"]
    if not valid_age and status in ("ok", "stale"):
        status = "event_time_unknown"
    remaining = snapshot["remaining_estimate"]
    result = {
        "status": status,
        "used_tokens": None if awaiting_usage else snapshot["last_request"]["total_tokens"],
        "window_tokens": snapshot["window_tokens"],
        "remaining_tokens": remaining["tokens"] if remaining is not None else None,
        "event_age_seconds": int(age) if valid_age else None,
    }
    if snapshot["compaction"]["threshold_tokens"] is not None:
        result["compaction_remaining_tokens"] = snapshot["compaction"]["remaining_estimate_tokens"]
    return result


def create_server(codex_home: str | Path | None = None):
    from mcp.server.fastmcp import FastMCP
    from mcp.types import ToolAnnotations

    server = FastMCP("codex-context-meter")

    @server.tool(
        name="get_context_usage",
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False,
        ),
    )
    # Any avoids a second, inferred structured payload on newer SDK versions.
    def get_context_usage(
        thread_id: str,
        stale_after_seconds: float = 120,
        auto_compact_token_limit: int | None = None,
    ) -> Any:
        """Check a context budget when it affects task planning; avoid routine polling.

        thread_id is the exact task UUID from the host or native executor CODEX_THREAD_ID;
        never infer it from cwd, the newest log, or the shared server environment.
        Returns JSON: status, used_tokens, window_tokens, remaining_tokens, event_age_seconds.
        Counts are last-request snapshots; even fresh log events can repeat old counters.
        stale means historical data; event_time_unknown means an unreliable timestamp.
        Null budgets are unknown, including while awaiting usage after compaction.
        A caller-supplied total-context threshold adds compaction_remaining_tokens.
        Reads local logs on demand, with no recorder. Full diagnostics are available in the CLI.
        """
        try:
            snapshot = read_context_usage(
                thread_id,
                codex_home=codex_home,
                stale_after_seconds=stale_after_seconds,
                auto_compact_token_limit=auto_compact_token_limit,
            )
            result = _compact_snapshot(snapshot)
        except ContextMeterError as exc:
            result = {"status": "unavailable", "error": {"code": exc.code, "message": str(exc)}}
        return json.dumps(result, separators=(",", ":"), ensure_ascii=True)

    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the read-only Codex context meter over MCP stdio.")
    parser.add_argument("--codex-home", help="Fixed Codex data directory for this server instance.")
    args = parser.parse_args(argv)
    try:
        server = create_server(args.codex_home)
    except ImportError:
        parser.exit(2, "Install MCP support first: pip install 'codex-context-meter[mcp]'\n")
    server.run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
