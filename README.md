# Codex MCP

English | [简体中文](README.zh-CN.md)

**Let Claude Code (or any MCP client) hand coding work to your local OpenAI Codex CLI, and keep a two-way conversation with it.**

Codex MCP is a small local MCP server. It turns the Codex CLI on your computer into a persistent coding subagent for your main AI agent:

- **Persistent conversations.** Start a task, send follow-ups, steer or interrupt it, and resume it after a restart.
- **Two-way communication.** Codex can ask the main agent questions (`ask_parent`), and the main agent answers them.
- **Safe approvals.** Nothing is approved silently. Approvals go to you, or to Codex's own automatic reviewer if you choose *Approve for me*.
- **Project-scoped sandbox.** Each task can only access the project directory it was given.
- **Native Codex features.** Subagents, plan mode, code review, forks, queues, history and more: 32 tools in total.
- **No extra dependencies.** Uses only the Python standard library. Works on Windows, macOS and Linux, using local pipes and sockets only (never a network port).

## Contents

- [Requirements](#requirements)
- [Installation](#installation)
- [Your first task](#your-first-task)
- [Approvals](#approvals)
- [Updating and uninstalling](#updating-and-uninstalling)
- [Troubleshooting](#troubleshooting)
- [How it works](#how-it-works)
- [Documentation](#documentation)
- [Development](#development)

## Requirements

| You need | Version | Check with |
|---|---|---|
| Python | 3.11 or newer | `python3 --version` (Windows: `py -3 --version`) |
| OpenAI Codex CLI | 0.167.0 or newer | `codex --version` |
| A Codex sign-in | ChatGPT account or API key | `codex login status` |
| An MCP client | for example Claude Code or Claude Desktop | |

Codex's own sandbox must work on your computer. The bridge never turns it off. On Windows, Codex's legacy `unelevated` sandbox is not supported; use the sandbox mode Codex sets up by default (see [installation details](docs/installation.md#windows-notes)).

## Installation

The steps below use Claude Code. For other clients, see [Step 5](#step-5-connect-your-mcp-client) and [docs/installation.md](docs/installation.md).

### Step 1: Install Python

- **Windows:** Install Python 3.11+ from [python.org](https://www.python.org/downloads/). In the installer, tick **Add python.exe to PATH**. This also installs the `py` launcher used below.
- **macOS:** `brew install python`, or use the installer from python.org.
- **Linux:** Use your distribution's package, for example `sudo apt install python3`.

### Step 2: Install and sign in to Codex

```bash
npm install -g @openai/codex
codex login
```

Any official installation method works. See [OpenAI's Codex CLI guide](https://developers.openai.com/codex/cli). Then check:

```bash
codex --version
codex login status
```

The version must be **0.167.0 or newer**. Run `codex update` to upgrade an older installation.

### Step 3: Download Codex MCP

Keep the folder in a permanent location (for example `~/tools/codex-mcp` or `C:\Tools\codex-mcp`), because your MCP client runs the server from there.

- **Git:**

  ```bash
  git clone https://github.com/houyhlouis/codex-mcp.git
  cd codex-mcp
  ```

- **GitHub Desktop:** Choose **File → Clone repository**, select `codex-mcp`, choose a local path, and click **Clone**.
- **ZIP:** On GitHub, click **Code → Download ZIP** and extract it.

The repository is private, so you must be signed in to GitHub with access to it.

### Step 4: Check that it works

Run these commands in the `codex-mcp` folder.

macOS / Linux:

```bash
python3 server.py --version
python3 server.py status
```

Windows (PowerShell):

```powershell
py -3 server.py --version
py -3 server.py status
```

`status` starts the background broker and Codex, then prints JSON that should include `"signed_in": true`. It does not run a model or use any quota. The broker keeps running in the background and is started again automatically when it is needed.

### Step 5: Connect your MCP client

**Claude Code.** Run this inside the `codex-mcp` folder.

macOS / Linux:

```bash
claude mcp add --transport stdio --scope user codex -- python3 "$PWD/server.py" serve
```

Windows (PowerShell):

```powershell
claude mcp add --transport stdio --scope user codex -- py -3 "$PWD\server.py" serve
```

`--scope user` makes the server available in all your projects. Leave it out to add it to the current project only. Check the connection with `claude mcp list`, or type `/mcp` inside Claude Code. `codex` should show as connected.

**Claude Desktop and other clients.** Add this to the client's MCP configuration file (for Claude Desktop: **Settings → Developer → Edit Config**), then restart the client:

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

On Windows use `"command": "py"` and `"args": ["-3", "C:\\Tools\\codex-mcp\\server.py", "serve"]`. Ready-made files: [examples/mcp.json](examples/mcp.json) and [examples/mcp.windows.json](examples/mcp.windows.json).

### Step 6 (optional): Teach your agent the workflow

Copy [examples/CLAUDE.md](examples/CLAUDE.md) into your project's `CLAUDE.md` (or your agent's instructions file). It explains how to poll for progress, answer questions and treat approvals.

No configuration file is needed. Optional settings, such as a custom Codex path or concurrency limit, are described in [docs/configuration.md](docs/configuration.md).

## Your first task

Ask your main agent in plain language:

```text
Use Codex in this project to find out why the tests fail and fix them.
Keep me updated and answer any questions it asks.
```

```text
Create a new project folder ~/projects/todo-cli and ask Codex to build a small
command-line to-do app with tests. Use "Approve for me" for this task.
```

```text
Continue the same Codex conversation: add a --json flag and run the tests again.
```

Behind the scenes, the main agent selects a project with `codex_projects`, starts work with `codex_start`, and polls `codex_events` until the turn finishes. It answers Codex's questions with `codex_respond` and reads the result with `codex_get`. See the [usage guide](docs/usage.md) for the full workflow and more examples.

## Approvals

Codex asks for approval before actions outside its sandbox, such as running a command that needs network access. You choose who decides for each task with `approval_mode`:

| Mode | Who decides |
|---|---|
| `inherit` (default) | Whatever your Codex configuration already uses |
| `ask` | You. The request is shown as a prompt in your MCP client, or you review it in a terminal |
| `approve_for_me` | Codex's built-in automatic reviewer. It can approve or deny; the sandbox stays on |

The main agent can always *deny* a request, but it cannot *approve* one on your behalf unless you explicitly enable `approval_relay` in the [configuration](docs/configuration.md). If your client cannot show prompts, review the request in a terminal:

```bash
python3 server.py review REQUEST_ID
```

## Updating and uninstalling

**Update:**

```bash
cd codex-mcp
git pull
python3 server.py stop
```

Then restart your MCP client. (Windows: `py -3 server.py stop`.) Always stop the broker after updating. Otherwise the client reports that a broker from a different version is still running.

**Uninstall:**

1. `python3 server.py stop`
2. `claude mcp remove codex --scope user` (or remove the entry from your client's configuration)
3. Delete the `codex-mcp` folder.
4. Optionally delete the saved task history: `~/.local/state/codex-mcp` on macOS/Linux, `%LOCALAPPDATA%\CodexMCP` on Windows.

## Troubleshooting

| Problem | What to do |
|---|---|
| `Codex executable not found` | Make sure `codex --version` works in a new terminal, or set `codex_binary` to its full path in a [config file](docs/configuration.md). |
| `codex` does not appear in `/mcp` | Check the path in your client's configuration and run the same command by hand, for example `python3 /path/to/server.py status`. |
| A broker from a different version or configuration is running | Run `python3 server.py stop` (with the same `--config` if you use one), then retry. |
| A task is stuck in `waiting` | It is waiting for an answer or approval. Ask the main agent to list pending requests, or run `server.py review REQUEST_ID`. |
| Something else | See [docs/troubleshooting.md](docs/troubleshooting.md). The private log is `broker.log` in the state folder. |

## How it works

```mermaid
flowchart LR
    A[Main agent<br/>e.g. Claude Code] <-->|MCP over stdio| B[server.py serve]
    B <-->|private local socket / pipe| C[Background broker]
    C <-->|app-server protocol| D[Codex CLI]
    D --> E[Your project]
```

The MCP server that your client starts is a thin adapter. A background *broker* owns the Codex connection, stores tasks and events in a private SQLite database, and keeps running when the client disconnects. A reconnecting client can catch up from where it left off. See [architecture](docs/architecture.md) and [security](docs/security.md).

## Documentation

| Guide | What it covers |
|---|---|
| [Installation details](docs/installation.md) | Other MCP clients, package install, Windows notes, running the broker manually |
| [Usage guide](docs/usage.md) | The task workflow, approvals, subagents, plan mode, forks, queues, recovery |
| [Tool reference](docs/tools.md) | All 32 tools, their arguments, task states and response formats |
| [Configuration](docs/configuration.md) | Optional settings file |
| [Troubleshooting](docs/troubleshooting.md) | Common errors and how to fix them |
| [Security](docs/security.md) | Trust model, sandbox, approvals and stored data |
| [Architecture](docs/architecture.md) | Components, message flow and delivery guarantees |
| [Limitations](docs/limitations.md) | What is intentionally not supported, and workarounds |
| [Development](docs/development.md) | Tests, CI, live checks and release packaging |
| [Changelog](CHANGELOG.md) | Release notes |

## Development

```bash
python3 -m unittest discover -v
```

The offline suite uses a simulated Codex process and uses no model quota. CI runs it on Windows, macOS and Linux with Python 3.11 and 3.14. See [docs/development.md](docs/development.md) and [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)
