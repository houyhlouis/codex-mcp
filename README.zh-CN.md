# Codex MCP

[English](README.md) | 简体中文

**让 Claude Code（或其他兼容的 MCP 客户端）把编程工作交给本机的 OpenAI Codex CLI，并与它保持双向对话。**

Codex MCP 是一个本地 MCP 服务器。它把你电脑上的 Codex CLI 变成主代理的持久编程子代理：

- **持久会话**：启动任务、发送后续指令、中途引导或中断，重启后还能恢复。
- **双向沟通**：Codex 可以通过 `ask_parent` 向主代理提问，主代理负责回答。
- **安全审批**：不会默默批准任何操作。审批请求交给你本人，或者在你选择 *Approve for me* 时交给 Codex 自带的自动审核器。
- **按项目隔离的沙箱**：Codex 运行的命令只能修改任务所在项目文件夹里的文件，未经你批准不能联网。在项目之外，它们只能读取程序运行所需的文件。详见[安全模型](docs/security.md#project-boundary-and-sandbox)（英文）。
- **Codex 原生功能**：子代理、计划模式、代码审查、分叉、排队、历史记录等，共 34 个工具。
- **并行工作与限额**：可选的 Git worktree 让并行任务各自拥有独立的检出目录，并交回可审核的补丁；小型计划按顺序启动相互依赖的步骤；任务组预算可以限制回合数、时间、token 和存储的输出。
- **无额外依赖**：只用 Python 标准库。支持 Windows、macOS 和 Linux，只用本地管道和套接字，从不监听网络端口。

## 目录

- [环境要求](#环境要求)
- [安装](#安装)
- [第一个任务](#第一个任务)
- [审批](#审批)
- [更新与卸载](#更新与卸载)
- [故障排除](#故障排除)
- [工作原理](#工作原理)
- [文档](#文档)
- [开发](#开发)

## 环境要求

| 需要 | 版本 | 检查命令 |
|---|---|---|
| Python | 3.11 或更新 | `python3 --version`（Windows：`py -3 --version`） |
| OpenAI Codex CLI | 0.167.0 或更新 | `codex --version` |
| Codex 登录 | ChatGPT 账号或 API 密钥 | `codex login status` |
| MCP 客户端 | Claude Code、Claude Desktop，或[其他 stdio 客户端](docs/installation.zh-CN.md#其他-mcp-客户端) | |

Codex 自身的沙箱必须能在你的电脑上正常工作，本桥接服务绝不会关闭它。在 Windows 上不支持 Codex 旧版的 `unelevated` 沙箱，请使用 Codex 默认设置的沙箱模式（见[安装细节](docs/installation.zh-CN.md#windows-说明)）。

## 安装

以下步骤以 Claude Code 为例。其他客户端请看[第 5 步](#第-5-步连接-mcp-客户端)和 [docs/installation.zh-CN.md](docs/installation.zh-CN.md)。

### 第 1 步：安装 Python

- **Windows**：从 [python.org](https://www.python.org/downloads/) 安装 Python 3.11 或更新版本，安装时勾选 **Add python.exe to PATH**。安装程序同时会装好下文使用的 `py` 启动器。
- **macOS**：`brew install python`，或使用 python.org 的安装包。
- **Linux**：使用发行版自带的软件包，例如 `sudo apt install python3`。

### 第 2 步：安装并登录 Codex

```bash
npm install -g @openai/codex
codex login
```

任何官方安装方式都可以，见 [OpenAI 的 Codex CLI 指南](https://developers.openai.com/codex/cli)。然后检查：

```bash
codex --version
codex login status
```

版本必须是 **0.167.0 或更新**。旧版本可运行 `codex update` 升级。

### 第 3 步：下载 Codex MCP

从 [codex-mcp 仓库](https://github.com/houyhlouis/codex-mcp)获取代码，并把文件夹放在一个固定位置（例如 `~/tools/codex-mcp` 或 `C:\Tools\codex-mcp`），因为 MCP 客户端会从这个路径启动服务器。仓库是公开的，不需要 GitHub 账号。

- **Git**：

  ```bash
  git clone https://github.com/houyhlouis/codex-mcp.git
  cd codex-mcp
  ```

- **GitHub Desktop**：选择 **File → Clone repository**，打开 **URL** 选项卡，输入 `https://github.com/houyhlouis/codex-mcp`，选择本地路径，点击 **Clone**。
- **ZIP**：在[最新发布页](https://github.com/houyhlouis/codex-mcp/releases/latest)下载 **Source code (zip)** 并解压，然后把解压出的文件夹重命名为 `codex-mcp`。

下载后不需要再安装任何东西，服务器直接从这个文件夹运行。如果你更想要一个 `codex-local-mcp` 命令，见[以 Python 包安装](docs/installation.zh-CN.md#以-python-包安装可选)。

### 第 4 步：确认可以运行

在 `codex-mcp` 文件夹中执行。

macOS / Linux：

```bash
python3 server.py --version
python3 server.py status
```

Windows（PowerShell）：

```powershell
py -3 server.py --version
py -3 server.py status
```

`status` 会启动后台 broker 和 Codex，并输出 JSON，其中应包含 `"signed_in": true`。它不会调用模型，也不消耗额度。broker 会在后台继续运行，需要时也会自动重新启动。

### 第 5 步：连接 MCP 客户端

**Claude Code**：在 `codex-mcp` 文件夹中执行。

macOS / Linux：

```bash
claude mcp add --transport stdio --scope user codex -- python3 "$PWD/server.py" serve
```

Windows（PowerShell）：

```powershell
claude mcp add --transport stdio --scope user codex -- py -3 "$PWD\server.py" serve
```

`--scope user` 表示在你所有项目中都可用；去掉它则只添加到当前项目。用 `claude mcp list` 或在 Claude Code 中输入 `/mcp` 检查连接，`codex` 应显示为已连接。

**Claude Desktop 和其他客户端**：把以下内容加入客户端的 MCP 配置文件（Claude Desktop：**Settings → Developer → Edit Config**），然后重启客户端：

```json
{
  "mcpServers": {
    "codex": {
      "command": "python3",
      "args": ["/absolute/path/to/codex-mcp/server.py", "serve"]
    }
  }
}
```

Windows 上请用 `"command": "py"` 和 `"args": ["-3", "C:\\Tools\\codex-mcp\\server.py", "serve"]`。现成的配置文件：[examples/mcp.json](examples/mcp.json) 和 [examples/mcp.windows.json](examples/mcp.windows.json)。

### 第 6 步（可选）：告诉主代理工作流程

把 [examples/CLAUDE.md](examples/CLAUDE.md) 复制到项目的 `CLAUDE.md`（或你的代理使用的说明文件）。它说明了如何轮询进度、回答问题和处理审批。

不需要任何配置文件。如需自定义 Codex 路径、并发上限等可选设置，见 [docs/configuration.zh-CN.md](docs/configuration.zh-CN.md)。

## 第一个任务

直接用自然语言告诉主代理：

```text
让 Codex 在当前项目中找出测试失败的原因并修复。
随时告诉我进度，并回答它提出的问题。
```

```text
新建项目文件夹 ~/projects/todo-cli，让 Codex 做一个带测试的命令行待办事项应用。
这个任务使用 "Approve for me"。
```

```text
继续刚才的 Codex 对话：增加 --json 参数，然后重新运行测试。
```

在后台，主代理会用 `codex_projects` 选择项目，用 `codex_start` 启动任务，并轮询 `codex_events` 直到本轮结束。它用 `codex_respond` 回答 Codex 的问题，用 `codex_get` 读取结果。完整流程和更多示例见[使用指南](docs/usage.zh-CN.md)。

## 审批

当 Codex 需要执行沙箱之外的操作（例如运行需要联网的命令）时，会先请求批准。每个任务可以用 `approval_mode` 选择由谁决定：

| 模式 | 由谁决定 |
|---|---|
| `inherit`（默认） | 沿用你的 Codex 配置 |
| `ask` | 你本人：请求会以提示框形式出现在 MCP 客户端中，或在终端中审核 |
| `approve_for_me` | Codex 内置的自动审核器。它可以批准或拒绝，沙箱始终有效 |

主代理随时可以*拒绝*请求，但除非你在[配置](docs/configuration.zh-CN.md)中明确开启 `approval_relay`，它无法替你*批准*。如果客户端无法显示提示框，可以在终端中审核：

```bash
python3 server.py review REQUEST_ID
```

## 更新与卸载

**更新**：

```bash
cd codex-mcp
python3 server.py stop
git pull
```

然后重启 MCP 客户端（Windows：`py -3 server.py stop`）。更新前一定要先停止旧的 broker，这样旧的 broker 不会加载新版本的文件；否则客户端还会提示仍有其他版本的 broker 在运行。请在没有任务工作时停止：被这次停止打断的任务会一直占用它的项目，直到你对它调用 `codex_resume`。

如果你是用 ZIP 安装的，请先运行 `python3 server.py stop`，再用 [Releases](https://github.com/houyhlouis/codex-mcp/releases) 中新的 **Source code (zip)** 替换原来的文件夹。

**卸载**：

1. `python3 server.py stop`
2. `claude mcp remove codex --scope user`（或从客户端配置中删除该条目）
3. 删除 `codex-mcp` 文件夹。
4. 如需删除保存的任务记录：macOS/Linux 为 `~/.local/state/codex-mcp`，Windows 为 `%LOCALAPPDATA%\CodexMCP`。

## 故障排除

| 问题 | 解决方法 |
|---|---|
| 不确定问题出在哪里 | 运行 `python3 server.py doctor`。它在不启动任何东西、不使用模型的情况下检查安装情况，并为每个问题给出下一步建议。见[故障排查](docs/troubleshooting.zh-CN.md#先运行-doctor)。 |
| `Codex executable not found` | 确认在新开的终端中 `codex --version` 能运行，或在[配置文件](docs/configuration.zh-CN.md)中把 `codex_binary` 设为完整路径。 |
| `/mcp` 中没有 `codex` | 检查客户端配置中的路径，并手动运行相同的命令，例如 `python3 /path/to/server.py status`。 |
| 提示有其他版本或配置的 broker 正在运行 | 运行 `python3 server.py stop`（如使用了 `--config`，请加上相同参数），然后重试。 |
| 任务一直处于 `waiting` | 它在等待回答或审批。让主代理列出待处理请求，或运行 `server.py review REQUEST_ID`。 |
| 其他问题 | 见 [docs/troubleshooting.zh-CN.md](docs/troubleshooting.zh-CN.md)。私有日志是状态文件夹中的 `broker.log`。 |

## 工作原理

```mermaid
flowchart LR
    A[主代理<br/>如 Claude Code] <-->|MCP over stdio| B[server.py serve]
    B <-->|私有本地套接字 / 命名管道| C[后台 broker]
    C <-->|app-server 协议| D[Codex CLI]
    D --> E[你的项目]
```

客户端启动的 MCP 服务器只是一个轻量适配层。后台 *broker* 持有与 Codex 的连接，把任务和事件保存在私有 SQLite 数据库中，客户端断开后也会继续运行。重新连接的客户端可以从上次的位置继续读取。详见[架构](docs/architecture.md)和[安全模型](docs/security.md)（英文）。

沙箱限制的是 Codex 运行的命令，而不是 Codex 本身：Codex 进程仍然要用你的登录信息和网络来访问模型。命令具体能读取和修改什么，见[安全模型](docs/security.md#project-boundary-and-sandbox)（英文）。

## 文档

| 文档 | 内容 |
|---|---|
| [安装细节](docs/installation.zh-CN.md) | 其他 MCP 客户端、以 Python 包安装、Windows 说明、手动运行 broker |
| [使用指南](docs/usage.zh-CN.md) | 任务流程、审批、子代理、计划模式、分叉、排队、恢复 |
| [配置](docs/configuration.zh-CN.md) | 可选的配置文件 |
| [故障排除](docs/troubleshooting.zh-CN.md) | 常见错误及解决方法 |
| [工具参考](docs/tools.md)（英文） | 全部 34 个工具的参数、任务状态和响应格式 |
| [安全模型](docs/security.md)（英文） | 信任模型、沙箱、审批和存储的数据 |
| [架构](docs/architecture.md)（英文） | 组件、消息流和投递保证 |
| [限制](docs/limitations.md)（英文） | 有意不支持的功能及替代方案 |
| [开发](docs/development.md)（英文） | 测试、CI、真实环境测试和发布打包 |
| [更新日志](CHANGELOG.md)（英文） | 版本说明 |

## 开发

```bash
python3 -m unittest discover -v
```

离线测试使用模拟的 Codex 进程，不消耗模型额度。每次推送后，[CI](https://github.com/houyhlouis/codex-mcp/actions/workflows/tests.yml) 都会在 Windows、macOS 和 Linux 上用 Python 3.11 和 3.14 运行这些测试。详见 [docs/development.md](docs/development.md) 和 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 许可证

[MIT](LICENSE)
