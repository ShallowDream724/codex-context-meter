"""Optional MCP stdio interface using the official Python SDK."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from .core import ContextMeterError, read_context_usage


def create_server(codex_home: str | Path | None = None):
    from mcp.server.fastmcp import FastMCP
    from mcp.types import ToolAnnotations

    server = FastMCP(
        "codex-context-meter",
        instructions=(
            "Read the latest recorded context usage of an explicitly identified local Codex thread. "
            "Use the actual thread UUID from the host or its native executor CODEX_THREAD_ID; "
            "never guess by working directory or choose the most recent session. "
            "The service process environment does not identify the caller's thread. "
            "Results are delayed snapshots, not live token counters. Check at meaningful task boundaries, "
            "especially before large reads or delegations. Compaction headroom is unknown unless an "
            "explicit total-context threshold is supplied."
        ),
    )

    @server.tool(
        name="get_context_usage",
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False,
        ),
    )
    def get_context_usage(
        thread_id: str,
        stale_after_seconds: float = 120,
        auto_compact_token_limit: int | None = None,
    ) -> dict[str, Any]:
        """Read an explicit thread's last-request usage, remaining-window estimate, and event freshness.

        thread_id must be the actual caller's Codex UUID. Counters are from the last
        recorded request, not cumulative billing. A fresh event can repeat old counters.
        auto_compact_token_limit, when known, must count the full active context.
        It is not inferred from model capacity or a fixed percentage.
        """
        try:
            return read_context_usage(
                thread_id,
                codex_home=codex_home,
                stale_after_seconds=stale_after_seconds,
                auto_compact_token_limit=auto_compact_token_limit,
            )
        except ContextMeterError as exc:
            return exc.as_dict()

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
