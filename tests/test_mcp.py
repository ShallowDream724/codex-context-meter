"""Exercise real MCP stdio framing with synthetic session data."""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

mcp = pytest.importorskip("mcp")
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

THREAD_ID = "00000000-0000-0000-0000-000000000007"


@pytest.mark.parametrize("resumed", [False, True])
def test_stdio_round_trip_and_explicit_thread_binding(tmp_path, resumed):
    folder = tmp_path / "sessions" / "2026" / "01" / "01"
    folder.mkdir(parents=True)
    log = folder / f"rollout-2026-01-01T00-00-00-{THREAD_ID}.jsonl"
    records = [
        {"type": "session_meta", "payload": {"id": THREAD_ID}},
        {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
                    "model_context_window": 1000,
                },
            },
        },
    ]
    log.write_text("".join(json.dumps(row) + "\n" for row in records), encoding="utf-8")
    if resumed:
        records[0]["payload"]["history_base"] = {
            "thread_id": THREAD_ID,
            "end_byte_offset": log.stat().st_size,
            "end_ordinal_exclusive": len(records),
        }
        records[1]["payload"]["info"]["last_token_usage"] = {
            "input_tokens": 200, "output_tokens": 20, "total_tokens": 220,
        }
        continuation = log.with_name(log.stem + "_00000000-0000-0000-0000-000000000008.jsonl")
        continuation.write_text("".join(json.dumps(row) + "\n" for row in records), encoding="utf-8")

    async def run():
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        env["CODEX_THREAD_ID"] = THREAD_ID
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "codex_context_meter.server", "--codex-home", str(tmp_path)],
            env=env,
        )
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                listing = await session.list_tools()
                assert [tool.name for tool in listing.tools] == ["get_context_usage"]
                tool = listing.tools[0]
                assert "thread_id" in tool.inputSchema["required"]
                assert "session_file" not in tool.inputSchema["properties"]
                assert tool.annotations.readOnlyHint is True
                assert tool.annotations.destructiveHint is False
                assert getattr(tool, "outputSchema", None) is None
                result = await session.call_tool("get_context_usage", {"thread_id": THREAD_ID})
                assert not result.isError
                assert len(result.content) == 1
                assert getattr(result, "structuredContent", None) is None
                data = json.loads(result.content[0].text)
                assert result.content[0].text == json.dumps(data, separators=(",", ":"))
                assert set(data) == {"status", "used_tokens", "window_tokens", "remaining_tokens", "remaining_percent", "event_age_seconds"}
                assert data["status"] == "ok"
                assert data["used_tokens"] == (220 if resumed else 120)
                assert data["remaining_tokens"] == (780 if resumed else 880)
                assert data["remaining_percent"] == (78.0 if resumed else 88.0)
                assert data["window_tokens"] == 1000
                assert isinstance(data["event_age_seconds"], int)
                assert str(tmp_path) not in json.dumps(data)
                missing = await session.call_tool("get_context_usage", {})
                assert missing.isError
                invalid = await session.call_tool("get_context_usage", {"thread_id": "../another-session"})
                failure = json.loads(invalid.content[0].text)
                assert failure["status"] == "unavailable"
                assert failure["error"]["code"] == "invalid_thread_id"
                assert failure["error"]["message"]
                assert set(failure) == {"status", "error"}

    asyncio.run(run())
