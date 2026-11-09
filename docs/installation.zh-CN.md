# 安装细节

[English](installation.md) | 简体中文 · [返回 README](../README.zh-CN.md)

大多数人只需按 [README](../README.zh-CN.md#安装) 中的分步指南安装。本页介绍其他选项。

- [服务器如何启动](#服务器如何启动)
- [MCP 客户端配置](#mcp-客户端配置)
- [以 Python 包安装（可选）](#以-python-包安装可选)
- [使用配置文件](#使用配置文件)
- [Windows 说明](#windows-说明)
- [手动运行 broker](#手动运行-broker)
- [验证安装](#验证安装)
- [更新](#更新)
- [卸载](#卸载)

## 服务器如何启动

MCP 客户端启动 `server.py serve`，并通过 stdin/stdout 与之通信。第一次真正调用工具时，`serve` 会启动后台 **broker** 进程（或连接到已经在运行的 broker）。broker 再启动 `codex app-server` 并保持其运行。

| 命令 | 用途 |
|---|---|
| `server.py serve` | MCP 服务器，写进客户端配置的就是这一条 |
| `server.py status` | 必要时启动 broker，并显示 Codex 版本和登录状态 |
| `server.py start` | 必要时启动 broker，并输出其进程 ID |
| `server.py stop` | 停止 broker，并等待它完全退出 |
| `server.py review REQUEST_ID` | 在交互式终端中回答审批或问题 |
| `server.py call TOOL --args JSON` | 直接调用 32 个工具之一，用于诊断 |
| `server.py --version` | 显示版本 |

`--config` 等全局选项要写在命令**之前**：`server.py --config /path/config.toml serve`。

## MCP 客户端配置

所有地方都请使用绝对路径，因为 MCP 客户端会在它自己的工作目录中启动服务器。

### Claude Code

```bash
# macOS / Linux，在 codex-mcp 文件夹中执行
claude mcp add --transport stdio --scope user codex -- python3 "$PWD/server.py" serve
```

```powershell
# Windows PowerShell，在 codex-mcp 文件夹中执行
claude mcp add --transport stdio --scope user codex -- py -3 "$PWD\server.py" serve
```

| 范围 | 含义 |
|---|---|
| `--scope user` | 在你所有的项目中可用 |
| `--scope local`（默认） | 只在当前项目中、只对你可用 |
| `--scope project` | 写入项目的 `.mcp.json`，与协作者共享。只有每个人的服务器都在相同路径时才适用 |

用 `claude mcp list` 或在 Claude Code 中输入 `/mcp` 检查。用 `claude mcp remove codex --scope user` 删除。

### Claude Desktop

打开 **Settings → Developer → Edit Config**，这会打开 `claude_desktop_config.json`（macOS 位于 `~/Library/Application Support/Claude/`，Windows 位于 `%APPDATA%\Claude\`）。添加服务器后重启 Claude Desktop：

```json
{
  "mcpServers": {
    "codex": {
      "command": "python3",
      "args": ["/Users/me/tools/codex-mcp/server.py", "serve"]
    }
  }
}
```

Windows：

```json
{
  "mcpServers": {
    "codex": {
      "command": "py",
      "args": ["-3", "C:\\Tools\\codex-mcp\\server.py", "serve"]
    }
  }
}
```

### 其他 MCP 客户端

任何支持 **stdio** 服务器的客户端都可以使用，配置如下：

- **命令**：`python3`（Windows：`py`）
- **参数**：`/absolute/path/to/codex-mcp/server.py serve`（Windows：`-3 C:\path\to\codex-mcp\server.py serve`）

服务器支持 MCP 协议版本 2024-11-05 至 2025-11-25。只支持更新版本的客户端可以通过兼容回退使用这些版本；CI 中测试了官方 MCP Python SDK 1.26.0。人工审批提示框需要客户端支持 **form elicitation**（MCP 2025-06-18 或更新）。不支持时，可在终端中审核。

## 以 Python 包安装（可选）

直接在文件夹中运行 `server.py` 无需安装。如果你希望在 `PATH` 中有一个命令：

```bash
cd codex-mcp
python3 -m pip install .
codex-local-mcp --version
```

然后在客户端配置中使用 `codex-local-mcp serve`，例如 `claude mcp add --transport stdio --scope user codex -- codex-local-mcp serve`。如果你的 Linux 发行版禁止全局 `pip`，请使用 `pipx install .` 或虚拟环境。更新源码后需要再运行一次 `pip install .`。

## 使用配置文件

配置文件是可选的，只在需要修改默认值（例如 Codex 路径、并发上限）时才创建，见[配置](configuration.zh-CN.md)。在客户端命令中传入：

```bash
claude mcp add --transport stdio --scope user codex -- python3 "$PWD/server.py" --config /absolute/path/config.toml serve
```

运行 `status`、`stop` 和 `review` 时也要使用同一个 `--config`。

## Windows 说明

- 如果 `python` 不在 `PATH` 中，请使用 `py` 启动器（`py -3`）。
- 如果 `codex` 是通过 npm 安装的，桥接服务会自动在 `codex.cmd` 启动脚本背后找到原生的 `codex.exe`。如果找不到，请在配置文件中把 `codex_binary` 设为 `codex.exe` 的完整路径。
- Codex 旧版的 **unelevated** Windows 沙箱无法阻止读取项目外的文件，因此桥接服务拒绝使用它。请使用 Codex 首次运行时默认设置的沙箱，见 OpenAI 的 [Windows 沙箱指南](https://learn.chatgpt.com/docs/windows/windows-sandbox)。
- 通信使用只有你的 Windows 账户才能打开的私有命名管道，不使用网络端口。
- 状态保存在 `%LOCALAPPDATA%\CodexMCP`。不需要 WSL。

## 手动运行 broker

有些 MCP 宿主程序退出时会结束所有子进程，后台 broker 也会随之被结束。如果每次关闭客户端任务都会停止，请先在普通终端中启动 broker：

```bash
python3 server.py start
```

之后 MCP 服务器会连接这个 broker，而不是自己启动一个。

## 验证安装

```bash
python3 server.py --version     # codex-local-mcp 0.1.1
python3 server.py status        # "signed_in": true，codex_version 为 0.167.0 或更新
python3 server.py call codex_models --args '{"limit": 3}'
```

以上命令都不会调用模型。离线测试套件不需要登录 Codex，见[开发文档](development.md)（英文）。

## 更新

```bash
cd codex-mcp
git pull                  # 或下载新的 ZIP / 在 GitHub Desktop 中点击 "Fetch origin" 和 "Pull"
python3 server.py stop    # 停止旧的 broker
```

之后重启 MCP 客户端。已保存的任务和历史会保留。如果跳过 `stop`，工具调用会报错 *"A broker started by a different version or configuration is still running"*，直到你停止旧 broker 为止。

## 卸载

1. `python3 server.py stop`
2. 从客户端中删除该服务器（`claude mcp remove codex --scope user`，或从 JSON 配置中删除）。
3. 如果以包的形式安装过：`python3 -m pip uninstall codex-local-mcp`。
4. 删除 `codex-mcp` 文件夹。
5. 如需删除状态文件夹（其中有任务历史、提示词和输出）：macOS/Linux 为 `~/.local/state/codex-mcp`（或 `$XDG_STATE_HOME/codex-mcp`），Windows 为 `%LOCALAPPDATA%\CodexMCP`。

Codex 自己的对话历史（位于 `~/.codex`）由 Codex 管理，不会被删除。
