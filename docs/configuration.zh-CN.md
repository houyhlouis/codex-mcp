# 配置

[English](configuration.md) | 简体中文 · [返回 README](../README.zh-CN.md)

**不需要任何配置文件。** 项目由主代理在运行时通过 `codex_projects` 或 `workdir` 参数选择，不需要维护允许使用的工作区列表。

只有在需要修改以下默认值时，才需要创建配置文件。

## 设置项

| 设置 | 默认值 | 说明 |
|---|---|---|
| `codex_binary` | `"codex"`（在 `PATH` 中查找） | Codex 可执行文件。如果 MCP 客户端看到的 `PATH` 与终端不同，请使用绝对路径。只写名称时只会在 `PATH` 中查找，绝不会使用当前文件夹中的文件 |
| `state_dir` | `~/.local/state/codex-mcp`（macOS/Linux，或 `$XDG_STATE_HOME/codex-mcp`）；`%LOCALAPPDATA%\CodexMCP`（Windows） | broker 的数据库、日志和套接字所在的私有文件夹。必须是专用的绝对路径，不能是主目录。在 macOS/Linux 上，完整套接字路径不能超过 100 字节 |
| `max_tasks` | `4` | 同时运行的独立任务组数量（1–32） |
| `toolchain_paths` | `[]` | 允许 Codex 沙箱**只读**访问的额外绝对路径，例如安装在特殊位置的工具链。系统运行时和 Codex 可执行文件本身已经可读 |
| `approval_relay` | `false` | 设为 `true` 时，主代理可以代你*批准*请求。除非你完全信任主代理，否则请保持关闭。*Approve for me* 不需要它 |
| `allow_external_tools` | `false` | 设为 `true` 时，Codex 任务可以使用你 Codex 配置中已有的外部 MCP 服务器和插件。它们运行在项目沙箱之外 |

示例（另见 [config.example.toml](../config.example.toml)）：

```toml
# ~/.config/codex-mcp/config.toml
codex_binary = "/usr/local/bin/codex"
max_tasks = 2
toolchain_paths = ["/opt/toolchains/node-22"]
```

在 Windows 上请使用 TOML 单引号字符串，这样反斜杠无需转义：

```toml
codex_binary = 'C:\Tools\codex\codex.exe'
```

路径可以以 `~` 开头，但 `$HOME`、`%USERPROFILE%` 等环境变量**不会**被展开。

## 使用配置文件

在所有运行服务器的地方，都要把 `--config` 写在命令**之前**：

```bash
python3 server.py --config ~/.config/codex-mcp/config.toml serve      # 写在 MCP 客户端配置中
python3 server.py --config ~/.config/codex-mcp/config.toml status
python3 server.py --config ~/.config/codex-mcp/config.toml stop
```

修改配置文件后，运行 `stop` 并重启 MCP 客户端。正在运行的 broker 会拒绝使用不同配置的客户端。

## 配置文件的要求

- 文件必须属于你本人，且其他用户不可写。在 macOS/Linux 上运行 `chmod 600 config.toml`。
- 不要把它放在交给 Codex 的任何项目文件夹中。Codex 无法访问配置文件和状态文件夹。
- 未知的设置项会被拒绝，因此拼写错误会被报告出来，而不是被默默忽略。

## 从早期开发版本升级

旧的 `[workspace_roots]` 段已被移除。请从配置文件中删除它，然后运行 `stop`。用 `workdir` 或 `project_id` 代替旧的 `root` 和相对 `path` 参数。已有任务会自动迁移，使用前会重新检查其目录。
