# Codex Context Meter

通过命令行或只读 MCP 工具，读取指定 Codex 本地任务最近一次已记录的上下文用量。

[English](README.md)

工具提供最近一次请求的 token 计数、日志记录的有效窗口、剩余空间估算和事件时间。它不会根据最近修改时间或工作目录猜测当前对话。

## 安装

需要 Python 3.10 或更新版本。命令行读取器仅依赖标准库；MCP 接口使用官方 Python SDK 的 v1 兼容线。

```bash
git clone https://github.com/ShallowDream724/codex-context-meter.git
cd codex-context-meter
python -m venv .venv
```

激活虚拟环境后安装：

```bash
python -m pip install ".[mcp]"
codex-context-meter --thread-id YOUR-CODEX-THREAD-UUID --pretty
```

Codex 原生终端提供 `CODEX_THREAD_ID` 时，可以省略 `--thread-id`。普通终端或其他 MCP 终端工具可能没有这个变量，此时必须明确传入实际任务 ID。

数据目录默认来自 `CODEX_HOME` 或 `~/.codex`。`--codex-home` 可覆盖目录，`--session-file` 可指定非标准位置的未压缩 JSONL 文件，但文件内的任务 ID 必须匹配。

## MCP 配置

在 Codex 的 `config.toml` 中加入配置，填写实际安装环境的 Python 路径：

```toml
[mcp_servers.context_meter]
command = "C:/path/to/.venv/Scripts/python.exe"
args = ["-m", "codex_context_meter.server"]
startup_timeout_sec = 10
tool_timeout_sec = 10
enabled_tools = ["get_context_usage"]
```

macOS 和 Linux 使用对应的 `.venv/bin/python` 路径。配置后重新加载 MCP，或开始新任务。

升级包后，需要重启 context-meter MCP 服务或其宿主，让已运行的 Python 进程载入新代码。

工具名为 `get_context_usage`，参数为：

| 参数 | 含义 |
| --- | --- |
| `thread_id` | 必填，实际调用方的 Codex 任务 UUID。 |
| `stale_after_seconds` | 事件超过多少秒标为陈旧，默认120秒。 |
| `auto_compact_token_limit` | 可选，已知的总上下文压缩阈值。默认未知。 |

共享 MCP 进程不能可靠地从自身环境识别调用它的对话，因此该接口不会自动使用服务器进程的 `CODEX_THREAD_ID`。任务 ID 应来自宿主信息或 Codex 原生终端环境；工具也不会选择“最新日志”作为替代。

## MCP 返回值

从0.2.0起，MCP 默认返回一份紧凑 JSON 文本，以下使用合成计数：

```json
{"status":"ok","used_tokens":201000,"window_tokens":353400,"remaining_tokens":152400,"event_age_seconds":4}
```

| 字段 | 含义 |
| --- | --- |
| `status` | 快照状态及不确定性。 |
| `used_tokens` | 最近一次请求的总 token 数，不使用累计费用计数。 |
| `window_tokens` | 同一日志事件记录的有效窗口。 |
| `remaining_tokens` | 窗口减去已用量的估算，最低为0。 |
| `event_age_seconds` | 日志事件距今的整秒数；时间未知或异常时为 `null`。 |

缓存和推理 token 不再次叠加。读数不包含尚未记录的新工作；较新的日志事件也可能重复旧计数，因此 `ok` 仍是快照。

- `stale`：日志超过 `stale_after_seconds`，计数仅作历史参考。
- `event_time_unknown`：时间戳缺失、无效或位于未来。压缩等待或窗口未知状态优先，事件年龄仍为 `null`。
- `awaiting_usage_after_compaction`：`used_tokens` 和 `remaining_tokens` 均为 `null`，直到观察到变化后的计数；仅重复旧计数不会恢复估算。扫描范围内没有压缩前基准时，需要另一个不同的用量记录。
- `window_unknown`：`window_tokens` 和 `remaining_tokens` 为 `null`。
- `unavailable`：返回 `status` 和含 `code`、`message` 的错误对象。

仅当调用方显式提供 `auto_compact_token_limit` 时，增加 `compaction_remaining_tokens`；压缩后等待新计数时该字段为 `null`。阈值必须按总上下文计算，不从模型容量或压缩后的新增量推算。

0.2.0 调整了 MCP 的返回格式，调用参数保持兼容。旧字段 `last_request.total_tokens` 对应 `used_tokens`，`remaining_estimate.tokens` 对应 `remaining_tokens`。默认结果省去重复说明、身份回显及非必要诊断，也不在 `structuredContent` 中重复返回。CLI 和 Python 库保留原有完整格式，可查询时间戳、分项计数及诊断信息。

适合在长任务的阶段边界、大范围读取或委派前按需检查。每次小工具调用后都检查会增加上下文本身的开销，也可能反复读取同一个快照。

## 范围与隐私

工具只按明确的 UUID，在配置目录的 `sessions` 和 `archived_sessions` 中查找文件。支持原始的 `rollout-...-THREAD_UUID.jsonl`，以及恢复任务后生成的 `rollout-...-THREAD_UUID_SEGMENT_UUID.jsonl`。每段日志中的任务 ID 都必须匹配，并通过 `history_base.thread_id` 构成唯一、连通的续接链，据此定位最后一段，不按文件修改时间选择。重复的分段 ID、分叉、循环、缺失前段或无效的历史引用均明确报错。最多定位128段，常规流程拒绝指向配置目录外的文件。

读取范围为各段首条元数据，以及从最后一段向前共享的尾部预算，默认8 MiB、最多64 MiB。继承的历史在 `history_base.end_byte_offset` 处截止，前段在此位置之后新增的记录不参与计算；压缩标记可跨分段识别。新分段暂无计数时，可以继承前段快照及其原始事件时间。`--session-file` 仍只读取指定的单个文件。工具不会联网或修改 Codex 文件，返回值不包含对话正文、工具输出、密钥或本机路径。

该工具适合本地账户使用，不承担不可信客户端之间的访问隔离；能调用它的客户端，可以查询配置目录内其他已知 UUID 的用量元数据。仅支持未压缩的 JSONL，会话只在云端或存储格式变化时可能无法读取。

更多参数、返回字段、开发命令和资料链接见 [英文说明](README.md)。采用 [MIT 许可证](LICENSE)。
