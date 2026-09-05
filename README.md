# Codex Context Meter

Read the latest recorded context-window usage of a local Codex thread through a CLI or a read-only MCP tool.

[中文说明](README.zh-CN.md)

The meter reads Codex rollout JSONL files. It reports the last request's token counters, the recorded effective window, a remaining-window estimate, and the age of the log event. It never selects a conversation by recency or working directory.

## Install

Python 3.10 or newer is required. The CLI uses only the standard library. MCP support uses the official Python SDK, pinned to its v1 compatibility line.

```bash
git clone https://github.com/ShallowDream724/codex-context-meter.git
cd codex-context-meter
python -m venv .venv
# Activate .venv with the command appropriate for your shell.
python -m pip install ".[mcp]"
```

For a pinned installation directly from GitHub:

```bash
python -m pip install "codex-context-meter[mcp] @ git+https://github.com/ShallowDream724/codex-context-meter.git@v0.1.1"
```

## CLI

```bash
codex-context-meter --thread-id YOUR-CODEX-THREAD-UUID --pretty
```

When a Codex native executor supplies `CODEX_THREAD_ID`, the CLI can omit `--thread-id`. Other terminals and MCP-based shell tools may not inherit that variable. In those environments, pass the actual thread UUID explicitly.

```bash
python -m codex_context_meter --pretty
```

`CODEX_HOME` defaults to `~/.codex`. Override it with `--codex-home`. For nonstandard storage, `--session-file` selects an exact uncompressed JSONL file; its `session_meta` ID must match the requested UUID.

Optional flags:

| Flag | Purpose |
| --- | --- |
| `--stale-after SECONDS` | Flag events older than this duration; default 120 seconds. |
| `--compact-threshold TOKENS` | Supply a known **total-context** auto-compaction threshold. |
| `--max-scan-bytes BYTES` | Bound the inspected tail; default 8 MiB, maximum 64 MiB. |
| `--pretty` | Indent the JSON response. |

Successful reads exit with code 0, including explicitly stale or post-compaction snapshots. A read or validation error returns JSON with `status: "unavailable"` and exits with code 2. Consumers must inspect `status` and freshness fields before using an estimate.

## Codex MCP Configuration

Add this section to your Codex `config.toml`, using the absolute path to the Python executable in the environment where you installed the package:

```toml
[mcp_servers.context_meter]
command = "/absolute/path/to/.venv/bin/python"
args = ["-m", "codex_context_meter.server"]
startup_timeout_sec = 10
tool_timeout_sec = 10
enabled_tools = ["get_context_usage"]
```

On Windows, use a path such as `C:/path/to/.venv/Scripts/python.exe`. A server-level `--codex-home` argument can select another fixed data directory. Reload the MCP configuration or start a new Codex task after adding the server.

After upgrading the package, restart the context-meter MCP server or its host so an already-running Python process loads the updated code.

The server exposes one read-only tool:

```text
get_context_usage(
    thread_id="00000000-0000-0000-0000-000000000001",
    stale_after_seconds=120,
    auto_compact_token_limit=null
)
```

The UUID above is synthetic. Supply the actual caller's thread ID from the host or its native executor's `CODEX_THREAD_ID`. The MCP tool deliberately requires this argument: a shared server process's environment does not reliably identify the calling conversation. It never lists unrelated sessions, exposes arbitrary file paths, or infers identity from the latest modified file.

## Interpret the Result

Example excerpt using synthetic counters:

```json
{
  "schema_version": 1,
  "status": "ok",
  "window_tokens": 353400,
  "last_request": {
    "input_tokens": 200000,
    "cached_input_tokens": 190000,
    "output_tokens": 1000,
    "total_tokens": 201000
  },
  "remaining_estimate": {
    "tokens": 152400,
    "percent": 43.1,
    "over_window_tokens": 0,
    "basis": "window_tokens - last_request.total_tokens"
  },
  "measurement_time_known": false
}
```

- `last_request` comes from `info.last_token_usage`. The cumulative `total_token_usage` is never used to calculate remaining context.
- `total_tokens` is used as recorded. Cached input and reasoning counters are not added a second time.
- `window_tokens` comes from that log record's `model_context_window`, not a guessed model limit or a fixed percentage.
- `event_at` and `event_age_seconds` describe the log event. Codex can repeat older counters in a newer event, so event freshness does not establish measurement freshness. `measurement_time_known` is always false.
- `remaining_estimate` excludes unreported work and is not a live measure of retained model state. Even an `ok` result is a snapshot.
- `status: "awaiting_usage_after_compaction"` withholds the estimate after a recognized compaction marker until changed usage counters appear. A newer timestamp repeating the same counters does not restore the estimate. If the bounded tail has no pre-compaction baseline, another distinct usage record is required.
- `status: "window_unknown"` withholds remaining-window estimates when the record does not provide a valid window.
- Compaction headroom stays unknown unless a caller supplies a known total-context threshold. Thresholds that count only growth after a compacted prefix are not interchangeable with total-context thresholds.

Check at meaningful task boundaries, before large reads or delegations, and when planning room for results and verification. Avoid polling after every small tool call: the check itself adds conversation tokens and snapshots may not have changed.

## Storage and Privacy

The resolver searches only filenames matching the supplied UUID beneath `CODEX_HOME/sessions` and `CODEX_HOME/archived_sessions`. It supports original `rollout-...-THREAD_UUID.jsonl` files and resumed `rollout-...-THREAD_UUID_SEGMENT_UUID.jsonl` files. Every segment's `session_meta` ID must match the requested thread. The `history_base.thread_id` references must form one connected chain; the final segment is selected by those references, never by modification time. Duplicate segment IDs, branches, cycles, missing predecessors, and invalid history references produce explicit errors. Discovery is limited to 128 segments. Files resolving outside `CODEX_HOME` are rejected by the normal resolver.

The reader inspects each segment's first metadata line and shares one tail budget across the chain, starting from the final segment. Inherited history ends at the recorded `history_base.end_byte_offset`; later records in a predecessor are excluded. This preserves compaction detection across segment boundaries. An empty continuation can inherit the prior snapshot, including its original event time. `--session-file` still reads only the exact selected file. The reader ignores malformed JSON and unfinished trailing records. It performs no network requests and does not modify Codex files. Tool output contains counters, timestamps, the supplied thread UUID, and diagnostic codes; it does not contain prompts, tool outputs, credentials, or local file paths.

This is a local convenience tool, not an authorization boundary between mutually untrusted clients. A client that can call it can request metadata for other known UUIDs within the configured data directory. Keep each server attached to the intended local account.

Uncompressed rollout JSONL is supported. Cloud-only sessions, compressed storage formats, and future schema changes may return unavailable results. Automatic compaction is not disabled, delayed, or triggered by this tool.

## Development

```bash
python -m pip install -e ".[dev]"
python -m pytest
python -m build
```

Tests use synthetic temporary rollouts. The MCP integration test starts a real stdio server and calls the tool through the official client SDK. No real conversations belong in fixtures or release artifacts.

## References

- [Codex App Server](https://learn.chatgpt.com/docs/app-server) documents `thread/tokenUsage/updated`. This package currently reads the local rollout log rather than attaching to an app-server transport.
- [Codex configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference) documents MCP launch settings and compaction threshold configuration.
- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk/tree/v1.x) provides the optional stdio transport.

MIT licensed. See [LICENSE](LICENSE).
