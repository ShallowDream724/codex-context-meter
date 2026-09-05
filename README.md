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
python -m pip install "codex-context-meter[mcp] @ git+https://github.com/ShallowDream724/codex-context-meter.git@v0.2.1"
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

The UUID above is synthetic. Supply the requested task's exact UUID. For the current task in Codex, use `nodeRepl.requestMeta.threadId` when exposed by `node_repl`; otherwise read `CODEX_THREAD_ID` through the native `exec_command` tool. MCP-based shells such as FastCtx `run` may expose their shared server's environment instead of the caller's. The meter requires an explicit UUID and never infers identity from a working directory or the latest modified file.

## Interpret the Result

Since 0.2.0, MCP returns one compact JSON text block. Example with synthetic counters:

```json
{"status":"ok","used_tokens":201000,"window_tokens":353400,"remaining_tokens":152400,"remaining_percent":43.1,"event_age_seconds":4}
```

| Field | Meaning |
| --- | --- |
| `status` | Snapshot availability and uncertainty, described below. |
| `used_tokens` | Last request's recorded total, never cumulative billing. |
| `window_tokens` | Effective window recorded in the same log event. |
| `remaining_tokens` | Estimated window minus used tokens, floored at zero. |
| `remaining_percent` | Remaining fraction of the recorded window, as 0-100%, rounded to one decimal. Added in 0.2.1. |
| `event_age_seconds` | Log event age in whole seconds, or `null` when unknown or invalid. |

Cached input and reasoning counters are not added again. Even an `ok` result excludes unreported work; a fresh log event can repeat older counters. Event age does not establish measurement freshness.

- `stale`: the log event exceeds `stale_after_seconds`; counts are historical.
- `event_time_unknown`: the timestamp is absent, invalid, or in the future. More restrictive compaction/window statuses take precedence; the age is still `null`.
- `awaiting_usage_after_compaction`: `used_tokens`, `remaining_tokens`, and `remaining_percent` are `null` until changed counters appear. A repeated count with a newer timestamp does not restore them. Without a pre-compaction baseline in the bounded tail, another distinct usage record is required.
- `window_unknown`: `window_tokens`, `remaining_tokens`, and `remaining_percent` are `null`.
- `unavailable`: the response contains `status` and an `error` object with a safe `code` and actionable `message`.

Only when the caller supplies `auto_compact_token_limit`, the response adds `compaction_remaining_tokens`. It is `null` while awaiting new usage after compaction. The supplied threshold must count total context; model capacity and thresholds that count only growth after a compacted prefix cannot substitute for it. `remaining_percent` always uses the recorded window, not the compaction threshold.

This replaces the MCP 0.1.x response format. Tool arguments are unchanged. The CLI and Python library retain their detailed version-1 schema, including timestamps, component counters, and diagnostics. For MCP consumers, `last_request.total_tokens` becomes `used_tokens`, and `remaining_estimate.tokens` becomes `remaining_tokens`. The MCP result omits repeated explanations and identity fields, and is not duplicated in `structuredContent`.

Check at meaningful task boundaries, before large reads or delegations, and when planning room for results and verification. Avoid polling after every small tool call: the check itself adds conversation tokens and snapshots may not have changed.

## Storage and Privacy

The resolver searches only filenames matching the supplied UUID beneath `CODEX_HOME/sessions` and `CODEX_HOME/archived_sessions`. It supports original `rollout-...-THREAD_UUID.jsonl` files and resumed `rollout-...-THREAD_UUID_SEGMENT_UUID.jsonl` files. Every segment's `session_meta` ID must match the requested thread. The `history_base.thread_id` references must form one connected chain; the final segment is selected by those references, never by modification time. Duplicate segment IDs, branches, cycles, missing predecessors, and invalid history references produce explicit errors. Discovery is limited to 128 segments. Files resolving outside `CODEX_HOME` are rejected by the normal resolver.

The reader inspects each segment's first metadata line and shares one tail budget across the chain, starting from the final segment. Inherited history ends at the recorded `history_base.end_byte_offset`; later records in a predecessor are excluded. This preserves compaction detection across segment boundaries. An empty continuation can inherit the prior snapshot, including its original event time. `--session-file` still reads only the exact selected file. The reader ignores malformed JSON and unfinished trailing records. It performs no network requests and does not modify Codex files. MCP returns budget counters, event age, and status or diagnostic codes; the CLI also includes timestamps and the supplied thread UUID. Neither returns prompts, tool outputs, credentials, or local file paths.

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
