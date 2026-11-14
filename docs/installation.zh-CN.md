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
- [已验证的范围](#已验证的范围)
- [备份与清理](#备份与清理)
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
| `server.py doctor` | 在不启动任何进程的情况下检查安装，见[故障排查](troubleshooting.zh-CN.md#先运行-doctor) |
| `server.py backup` | 保存一份状态数据库副本，见[备份与清理](#备份与清理) |
| `server.py prune` | 先显示计划，再从状态数据库中删除旧数据 |
| `server.py incident-export` | 为问题报告导出某个任务的结构（不含内容），见[报告问题](troubleshooting.zh-CN.md#报告问题) |
| `server.py worktree` | 在终端中准备、检查、交付或删除 Git worktree，与 [`codex_worktrees`](tools.md#worktrees)（英文）使用同一个辅助程序。请在没有 Codex 任务在主检出目录中工作时使用 |
| `server.py call TOOL --args JSON` | 直接调用 34 个工具之一，用于诊断 |
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

只要客户端能启动 **stdio** 服务器，并且使用下面列出的某个 MCP 版本，就可以使用。配置如下：

- **命令**：`python3`（Windows：`py`）
- **参数**：`/absolute/path/to/codex-mcp/server.py serve`（Windows：`-3 C:\path\to\codex-mcp\server.py serve`）

**支持的 MCP 版本**：2024-11-05、2025-03-26、2025-06-18、2025-11-25 和 2026-07-28。在 2026-07-28 版本上只支持 stdio 传输，并可使用可选的 Tasks 扩展，详见[工具参考](tools.md#mcp-2026-07-28-and-tasks)。

**审批提示框**只有在客户端支持 *form elicitation*（MCP 2025-06-18 或更新）时才会显示在客户端里。使用其他客户端时，请在终端运行 `python3 server.py review REQUEST_ID` 来批准请求，其余功能不受影响。

**已测试的范围**见[已验证的范围](#已验证的范围)。

## 以 Python 包安装（可选）

直接在文件夹中运行 `server.py` 无需安装。如果你希望在 `PATH` 中有一个命令，可以从下载的文件夹安装这个包：

```bash
cd codex-mcp
python3 -m pip install .
codex-local-mcp --version
```

然后在客户端配置中使用 `codex-local-mcp serve`，例如 `claude mcp add --transport stdio --scope user codex -- codex-local-mcp serve`。如果你的 Linux 发行版禁止全局 `pip`，请使用 `pipx install .` 或虚拟环境。更新源码后需要再运行一次 `pip install .`。

注意这三个名字：仓库叫 `codex-mcp`，Python 包和它的命令叫 `codex-local-mcp`，Python 模块叫 `codex_local_mcp`。这个包没有发布到 PyPI，所以 `pip install codex-local-mcp` 不能用，请始终从文件夹安装。

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

### WSL

Codex MCP 要么完全运行在 Windows 上，要么完全运行在 WSL 内。服务器、Python、Codex CLI 和项目目录必须位于同一个环境中，因为沙箱和路径检查都属于 Codex 所运行的那个系统。

- **全部在 WSL 中，客户端也在 WSL 中**（例如从 WSL 终端启动的 Claude Code）：按照 Linux 的说明操作。
- **全部在 WSL 中，但客户端在 Windows 上**（例如 Claude Desktop）：让客户端通过 `wsl.exe` 启动服务器：

  ```json
  {
    "mcpServers": {
      "codex": {
        "command": "wsl.exe",
        "args": ["python3", "/home/me/tools/codex-mcp/server.py", "serve"]
      }
    }
  }
  ```

  现成的配置文件见 [examples/mcp.wsl.json](../examples/mcp.wsl.json)。项目目录要使用 Linux 路径，例如 `/home/me/projects/demo`。项目应放在 Linux 文件系统中，不要放在 `/mnt/c` 下，那里的文件权限行为不同。`server.py review`、`status`、`stop` 和 `doctor` 也要在 WSL 内运行。
- **不要混用。** Windows 上的服务器不能使用安装在 WSL 中的 Codex，反过来也不行。如果 `doctor` 在你的终端里通过，而客户端却失败，请检查客户端是在哪个环境中启动服务器的。

以上 WSL 用法是根据服务器的工作方式推导出来的，维护者没有实际试过。

## 手动运行 broker

有些 MCP 宿主程序退出时会结束所有子进程，后台 broker 也会随之被结束。如果每次关闭客户端任务都会停止，请先在普通终端中启动 broker：

```bash
python3 server.py start
```

之后 MCP 服务器会连接这个 broker，而不是自己启动一个。

## 验证安装

```bash
python3 server.py --version     # codex-local-mcp 0.2.1
python3 server.py status        # "signed_in": true，codex_version 为 0.167.0 或更新
python3 server.py call codex_models --args '{"limit": 3}'
```

以上命令都不会调用模型。离线测试套件不需要登录 Codex，见[开发文档](development.md)（英文）。

如果想做一次不启动任何东西的检查，请运行 `python3 server.py doctor`，见[故障排查](troubleshooting.zh-CN.md#先运行-doctor)。

`status` 的输出中还有 `compatibility` 一项。`tested` 表示你的 Codex 版本是本项目测试过的版本；`untested` 表示预发布版本或更新的版本，通常可以正常使用，但没有经过验证。

## 已验证的范围

“能用”可以有不同的含义，所以下面每一行都说明了它是怎样验证的。

| 级别 | 含义 |
|---|---|
| **协议** | 用模拟的 Codex，自动对照 MCP 规范或 Codex 自己的 schema 检查消息 |
| **平台** | 离线测试套件在该操作系统和 Python 版本上运行过 |
| **真实运行** | 用真实的 Codex 账户运行过 |
| **客户端** | 通过该客户端的真实界面使用过 |

| 组合 | 验证级别 | 说明 |
|---|---|---|
| MCP 2024-11-05、2025-03-26、2025-06-18、2025-11-25 | 协议 | 通过 stdio 测试。CI 还会用官方 MCP Python SDK 1.26.0 对服务器进行测试 |
| MCP 2026-07-28（含 Tasks） | 协议 | 只对照已发布的规范和 schema 检查过，尚未用 SDK 或真实客户端验证 |
| Codex CLI 0.167.0 | 协议、真实运行 | 服务器的消息会对照 Codex 生成的 schema 进行验证。更旧的版本会被拒绝，更新的版本会标记为 `untested` |
| Linux，Python 3.11 和 3.14 | 平台、真实运行 | 真实运行测试使用 ChatGPT 登录 |
| Windows 和 macOS，Python 3.11 和 3.14 | 平台 | 每次推送后由 CI 运行。v0.1.1 在两个系统上都通过了，0.2.1 版本尚未在这两个系统上运行 |
| Linux 上的 Claude Code | 客户端 | 曾配合 v0.1.1 长时间使用：启动任务、后续指令、回答 Codex 的问题、*Approve for me*、重启后恢复。它的审批提示框没有使用过 |
| Claude Desktop 及其他客户端 | 未验证 | 它们使用相同的协议，预计可以正常工作 |
| WSL | 未验证 | 见 [WSL](#wsl) |
| 使用 API key 登录 | 未验证 | |

协议级别的检查说明服务器的通信是正确的，但不能说明某个客户端如何显示提示框，或如何处理长时间的等待。这张表背后带日期的结果见[验证状态](development.md#validation-status)（英文）。

## 备份与清理

状态目录会随着每个任务增长，其中的内容不会被自动删除。有两个命令可以用来管理它。主代理无法使用这两个命令，需要你自己在终端里运行。

### 备份

```bash
python3 server.py backup
```

这会在状态目录中写入一份数据库副本，文件名为 `state.sqlite3.backup-<数字>.bak`，检查副本后输出其路径。broker 运行时也可以执行，不会影响正在进行的任务。

如果要把副本写到别处，加上 `--destination /绝对路径/file.bak`。目标目录必须只有你的账户可以访问，并且不能位于项目目录内。已存在的文件不会被覆盖。这样生成的备份会一直保留，直到你自己删除。备份里有你的提示词和 Codex 的输出，请像对待状态目录一样对待它。

### 清理旧数据

清理分两步进行，这样在删除任何内容之前，你可以先看到将要删除什么。

1. 停止 broker，然后生成清理计划：

   ```bash
   python3 server.py stop
   python3 server.py prune --older-than 30d
   ```

   计划会列出将删除多少数据、有多少数据受保护及其原因，并给出一个计划 ID。此时还没有任何改动。`--older-than` 支持 `s`、`m`、`h`、`d`、`w`；不带单位的数字表示天数。加上 `--max-bytes N`，则只删除必要的旧数据，使保存的数据降到 `N` 字节以下。

2. 执行计划：

   ```bash
   python3 server.py prune --apply --plan-id PLAN_ID
   ```

   执行前会重新检查数据。如果生成计划之后有任何变化，或者计划已超过一小时，就会被拒绝，需要重新生成。最多可以同时保留 32 个计划，旧的计划会自动清除。加上 `--compact` 可以在之后压缩数据库文件；不加的话，释放的空间会留给新数据使用。

broker 运行时 `prune` 会拒绝执行，它也绝不会自己去停止 broker。两个命令都支持 `--json`。

**会被删除的内容：**旧事件、已完成操作保存的结果、已关闭的问题和审批、回合结果及其附件、已结束任务的委派约定、已过期的 MCP 任务记录，以及从未发送给 Codex 的图片。

**绝不会被删除的内容：**

- 属于仍在运行、等待或排队的任务的任何内容，以及有未处理问题或审批的任务的任何内容；
- 项目占用记录或结果未知的操作仍然依赖的任何内容；
- 任务和项目列表本身；
- 已发送给 Codex 的图片，因为 Codex 自己的历史记录可能仍在引用它们；
- 操作键已被使用的记录。结果被删除后，这个键仍然保持锁定，因此重复调用绝不会让工作执行两次；
- `~/.codex` 中 Codex 自己的历史记录，以及你项目中的所有文件。

清理之后，旧的事件游标或旧的操作键会得到明确的错误（`cursor_expired`、`operation_expired`），而不是错误的数据。见[工具参考](tools.md#error-codes)。

**刚从 0.1.x 更新之后，**计划可能会提示有些任务在升级后尚未重新检查，并保护所有数据。这时启动一次服务器（`python3 server.py start`），稍等片刻让它向 Codex 核对旧任务，然后停止它，再重新生成计划。

## 更新

```bash
cd codex-mcp
python3 server.py stop    # 先停止旧的 broker
git pull                  # 或下载新的 ZIP / 在 GitHub Desktop 中点击 "Fetch origin" 和 "Pull"
```

请在替换文件之前停止 broker，这样旧的 broker 不会加载新版本的文件；并且要在没有任务工作时停止。`stop` 会结束正在运行的回合，但不一定会结束这些回合启动的命令；停止时仍在工作的任务，之后会一直占用它的项目，直到你对它调用 `codex_resume`（见[仍在运行的命令](troubleshooting.zh-CN.md#仍在运行的命令)）。

之后重启 MCP 客户端。已保存的任务和历史会保留。如果跳过 `stop`，工具调用会报错 *"A broker started by a different version or configuration is still running"*，直到你停止旧 broker 为止。

## 卸载

1. `python3 server.py stop`
2. 从客户端中删除该服务器（`claude mcp remove codex --scope user`，或从 JSON 配置中删除）。
3. 如果以包的形式安装过：`python3 -m pip uninstall codex-local-mcp`。
4. 删除 `codex-mcp` 文件夹。
5. 如需删除状态文件夹（其中有任务历史、提示词和输出）：macOS/Linux 为 `~/.local/state/codex-mcp`（或 `$XDG_STATE_HOME/codex-mcp`），Windows 为 `%LOCALAPPDATA%\CodexMCP`。

Codex 自己的对话历史（位于 `~/.codex`）由 Codex 管理，不会被删除。
