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

工具名为 `get_context_usage`，参数为：

| 参数 | 含义 |
| --- | --- |
| `thread_id` | 必填，实际调用方的 Codex 任务 UUID。 |
| `stale_after_seconds` | 事件超过多少秒标为陈旧，默认120秒。 |
| `auto_compact_token_limit` | 可选，已知的总上下文压缩阈值。默认未知。 |

共享 MCP 进程不能可靠地从自身环境识别调用它的对话，因此该接口不会自动使用服务器进程的 `CODEX_THREAD_ID`。任务 ID 应来自宿主信息或 Codex 原生终端环境；工具也不会选择“最新日志”作为替代。

## 如何理解读数

- 仅使用 `last_token_usage` 计算当前请求口径的余量，累计费用计数 `total_token_usage` 不参与计算。
- 缓存和推理 token 不会再次叠加到已报告的总量中。
- 有效窗口来自日志中的 `model_context_window`，不根据模型名称、配置上限或固定系数猜测。
- `remaining_estimate` 是最近一次已记录请求的剩余窗口估算，未包含尚未记录的新工作。
- `event_age_seconds` 是日志事件的年龄。较新的事件也可能重复旧计数，实际测量时间不可确认，因此 `measurement_time_known` 始终为 `false`。
- 出现压缩标记后，状态为 `awaiting_usage_after_compaction`，暂不提供余量，直到观察到变化后的用量计数；仅更新时间戳并重复旧计数不会恢复估算。扫描范围内没有压缩前基准时，需要等到另一个不同的用量记录。
- 窗口未知时返回 `window_unknown`。缺失日志或无有效用量等错误返回 `unavailable`，不会伪造百分比。
- 自动压缩阈值默认未知；显式提供的阈值必须按总上下文计算。按压缩后新增内容计算的阈值不能直接代入。

适合在长任务的阶段边界、大范围读取或委派前按需检查。每次小工具调用后都检查会增加上下文本身的开销，也可能反复读取同一个快照。

## 范围与隐私

工具只按明确的 UUID，在配置目录的 `sessions` 和 `archived_sessions` 中查找文件名，随后验证日志中的任务 ID。多份匹配文件会报告歧义。常规定位流程拒绝指向配置目录外的文件。

读取范围为选定文件的首条元数据和有大小上限的尾部，默认尾部8 MiB、最多64 MiB。工具不会联网或修改 Codex 文件，返回值不包含对话正文、工具输出、密钥或本机路径。

该工具适合本地账户使用，不承担不可信客户端之间的访问隔离；能调用它的客户端，可以查询配置目录内其他已知 UUID 的用量元数据。仅支持未压缩的 JSONL，会话只在云端或存储格式变化时可能无法读取。

更多参数、返回字段、开发命令和资料链接见 [英文说明](README.md)。采用 [MIT 许可证](LICENSE)。
