# 故障排除

[English](troubleshooting.md) | 简体中文 · [返回 README](../README.zh-CN.md)

请在 `codex-mcp` 文件夹中运行以下命令。Windows 上用 `py -3` 代替 `python3`。如果使用了配置文件，请在命令前加上 `--config /path/config.toml`。

## 先做这些检查

```bash
python3 --version              # 3.11 或更新
codex --version                # 0.167.0 或更新
codex login status             # 已登录
python3 server.py --version
python3 server.py status       # 启动 broker；应显示 "signed_in": true
```

`codex doctor` 可以检查 Codex 本身的安装情况。broker 的私有日志是状态文件夹中的 `broker.log`：macOS/Linux 为 `~/.local/state/codex-mcp/`，Windows 为 `%LOCALAPPDATA%\CodexMCP\`。

## 常见问题

| 现象 | 原因与解决方法 |
|---|---|
| `Codex executable not found` | MCP 客户端看到的 `PATH` 中没有 Codex。在[配置文件](configuration.zh-CN.md)中把 `codex_binary` 设为绝对路径。 |
| `Codex 0.167.0 or newer is required` | 运行 `codex update`，然后运行 `python3 server.py stop`。 |
| `/mcp` 或客户端的服务器列表中没有 `codex`，或显示失败 | 在终端中运行客户端配置里的那条命令（把 `serve` 换成 `status`），查看错误信息。确认 `server.py` 的路径是绝对路径。 |
| `A broker started by a different version or configuration is still running` | 你更新了代码或修改了设置。运行 `python3 server.py stop`（如有 `--config` 请加上相同参数），然后重试。 |
| `Broker failed to start` / `did not become ready` | 查看状态文件夹中的 `broker.log`。某些受限容器禁止使用 Unix 套接字。 |
| `Work directory ... missing` / `must be an absolute path` | 传入绝对路径，或让主代理用 `codex_projects`（`action: "create"`）创建文件夹。 |
| `Select a dedicated project directory, not the home directory` | 请选择一个项目子文件夹。整个主目录或盘符根目录不能作为项目。 |
| `Project directory overlaps private state` | 项目包含状态文件夹或配置文件，或位于它们之中。请换一个文件夹。 |
| `Another task is active in this project` | 同一文件夹中同一时间只能有一个独立任务。等待、中断它，或使用其他文件夹 / worktree。 |
| `Concurrent task limit reached` | 结束或中断一个任务，或者调大 `max_tasks`。 |
| `Configuration must be owned by this user...` | 对配置文件执行 `chmod 600`（macOS/Linux）。在 Windows 上，确认只有你的账户可以修改它。 |
| `State directory must be owned by this user with mode 0700` | 对状态文件夹执行 `chmod 700`，或选择新的 `state_dir`。 |
| `state_dir is too long for a Unix socket path` | 选择更短的 `state_dir`。 |
| `The legacy unelevated Windows sandbox cannot enforce denied reads` | 把 Codex 切换回默认的 Windows 沙箱（见[安装细节](installation.zh-CN.md#windows-说明)）。 |
| Codex 内部提示某个工具"not found"（例如装在特殊位置的编译器） | 把它所在的文件夹加入 `toolchain_paths`，然后 `stop` broker。 |
| `Model is not in the visible Codex catalog` / `Unsupported reasoning effort` | 调用 `codex_models`，使用其中列出的组合。 |
| `Previous operation with this request_id is uncertain` | 这个键已经用过，且那次操作失败或被打断。检查任务后，换一个新的 `request_id`。 |
| `request_id was already used with different arguments` | 每个新操作都要使用新的、唯一的 `request_id`。 |

## 任务处于等待状态

`waiting` 状态的任务需要一个回答。

1. 用 `codex_requests` 列出待处理请求（或读取 `codex_get` 中的 `pending_requests`）。
2. 对于 `parent_question`，主代理用 `codex_respond` 和 `{"text": "..."}` 回答。
3. 对于审批和用户问题，使用 `codex_review_request`。如果客户端无法显示提示框，在终端中运行 `python3 server.py review REQUEST_ID`。
4. Codex 重启后请求会过期。如果回答因过期被拒绝，请调用 `codex_resume`，让 Codex 重新发起请求。

如果取消子代理的审批后父任务一直在等待，请中断父任务的这一轮，等待 `interrupted`，再发送一条说明下一步做法的后续指令。

## 关闭客户端后任务就停止了

有些 MCP 宿主程序退出时会结束所有子进程，broker 也会被结束。请在打开客户端之前，先在普通终端中运行 `python3 server.py start` 启动 broker。

## 崩溃或重启后的恢复

- 正在进行的任务会变为 `needs_resume`。调用 `codex_resume`，阅读 `codex_history`，再发送后续指令。不会自动重放任何内容。
- 从上次保存的游标继续读取 `codex_events`，已提交的事件不会丢失。
- `uncertain` 状态表示某个操作的确认丢失了。重复任何操作之前，先查看 `codex_get` 和 `codex_history`。
- 如果排队的消息提示"is no longer pending"，说明 Codex 很可能已经开始执行它。请检查任务，不要重复添加。

## 报告问题

请附上 `python3 server.py --version`、`codex --version` 的输出、操作系统、失败的工具调用以及（已脱敏的）错误信息，并说明离线测试（`python3 -m unittest discover`）是否通过。不要上传状态文件夹、`~/.codex` 或完整的对话历史，其中可能包含私有代码和提示词。安全问题请参见 [SECURITY.md](../SECURITY.md)。
