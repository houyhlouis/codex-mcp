# 配置说明

[English](configuration.md) | 简体中文 · [中文首页](../README.zh-CN.md)

默认使用不需要配置文件。主代理通过 `codex_projects` 选择或创建项目，或者向 `codex_start` 直接传入绝对路径 `workdir`。配置文件中没有工作区列表。

例如，调用 `codex_projects` 创建目录：

```json
{"action":"create","path":"C:\\Work\\demo"}
```

启动任务时可以使用返回的 `project_id`，也可以指定已有目录：

```json
{"workdir":"/Users/me/projects/demo","prompt":"实现所需功能并运行测试。","request_id":"feature-001"}
```

每个任务只获得所选项目范围内的权限。服务拒绝文件系统根目录、整个用户主目录、私有状态或凭据目录，以及符号链接和 Windows 目录联接。macOS 的 `/tmp` 等系统别名会被规范化。受信任的主代理负责选择正确的项目；目录选择本身就是一次明确的访问授权。

## 可选设置

需要调整默认值时，在命令前传入 `--config /absolute/settings.toml`。字段示例见 [config.example.toml](../config.example.toml)。

| 设置 | 用途 |
|---|---|
| `codex_binary` | Codex 原生可执行文件路径，或 `PATH` 中的命令名 |
| `state_dir` | 私有后台状态目录；Windows 默认为 `%LOCALAPPDATA%/CodexMCP`，Unix 默认为 `~/.local/state/codex-mcp` |
| `max_tasks` | 独立任务组并发上限，取值 1–32，默认 4 |
| `toolchain_paths` | 操作者额外允许只读访问的工具链路径 |
| `approval_relay` | 是否允许受信任的主代理转发审批决定；默认关闭 |
| `allow_external_tools` | 是否允许已有外部集成；默认关闭 |

真实设置文件应放在编程任务目录之外。原生 **Approve for me** 通过每个任务的 `approval_mode` 选择，与 `approval_relay` 是不同的机制。

## 较早的开发配置

使用原设置文件停止旧后台进程，删除 `[workspace_roots]`，然后重新启动。旧工具参数 `root` 和相对 `path` 应替换为 `workdir` 或 `project_id`。此前保存的任务目录会迁入项目登记表，并在使用前重新校验；提示不会自动重放。
