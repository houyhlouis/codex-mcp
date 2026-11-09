# Installation details

English | [简体中文](installation.zh-CN.md) · [Back to README](../README.md)

The [README](../README.md#installation) has the step-by-step installation guide for most people. This page covers the other options.

- [How the server is started](#how-the-server-is-started)
- [MCP client configurations](#mcp-client-configurations)
- [Installing as a Python package (optional)](#installing-as-a-python-package-optional)
- [Using a configuration file](#using-a-configuration-file)
- [Windows notes](#windows-notes)
- [Running the broker yourself](#running-the-broker-yourself)
- [Verifying an installation](#verifying-an-installation)
- [Updating](#updating)
- [Uninstalling](#uninstalling)

## How the server is started

Your MCP client launches `server.py serve` and talks to it over stdin/stdout. On the first real tool call, `serve` starts a background **broker** process (or connects to the one already running). The broker launches `codex app-server` and keeps it running.

| Command | Purpose |
|---|---|
| `server.py serve` | The MCP server. Put this in your client configuration. |
| `server.py status` | Start the broker if needed and show Codex version and sign-in status. |
| `server.py start` | Start the broker if needed and print its process ID. |
| `server.py stop` | Stop the broker and wait until it has exited. |
| `server.py review REQUEST_ID` | Answer an approval or question in an interactive terminal. |
| `server.py call TOOL --args JSON` | Call one of the 32 tools directly, for diagnosis. |
| `server.py --version` | Print the version. |

Global options such as `--config` go **before** the command: `server.py --config /path/config.toml serve`.

## MCP client configurations

Use absolute paths everywhere. MCP clients start servers from their own working directory.

### Claude Code

```bash
# macOS / Linux, run inside the codex-mcp folder
claude mcp add --transport stdio --scope user codex -- python3 "$PWD/server.py" serve
```

```powershell
# Windows PowerShell, run inside the codex-mcp folder
claude mcp add --transport stdio --scope user codex -- py -3 "$PWD\server.py" serve
```

| Scope | Meaning |
|---|---|
| `--scope user` | Available in all your projects |
| `--scope local` (default) | Only in the current project, only for you |
| `--scope project` | Written to the project's `.mcp.json` and shared with collaborators. Only use it if everyone has the server at the same path. |

Check with `claude mcp list` or `/mcp` inside Claude Code. Remove with `claude mcp remove codex --scope user`.

### Claude Desktop

Open **Settings → Developer → Edit Config**. This opens `claude_desktop_config.json`, which is in `~/Library/Application Support/Claude/` on macOS and `%APPDATA%\Claude\` on Windows. Add the server and restart Claude Desktop:

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

On Windows:

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

### Other MCP clients

Any client that supports **stdio** servers works. Configure:

- **command:** `python3` (Windows: `py`)
- **arguments:** `/absolute/path/to/codex-mcp/server.py serve` (Windows: `-3 C:\path\to\codex-mcp\server.py serve`)

The server supports MCP protocol versions 2024-11-05 through 2025-11-25. Clients that only speak a newer version can use their fallback to these versions; the official MCP Python SDK 1.26.0 is tested in CI. Human approval prompts need a client with **form elicitation** support (MCP 2025-06-18 or newer). Without it, approvals are reviewed in a terminal.

## Installing as a Python package (optional)

Running `server.py` from the folder needs no installation. If you prefer a command on your `PATH`:

```bash
cd codex-mcp
python3 -m pip install .
codex-local-mcp --version
```

Then use `codex-local-mcp serve` in your client configuration, for example `claude mcp add --transport stdio --scope user codex -- codex-local-mcp serve`. On Linux distributions that block system-wide `pip`, use `pipx install .` or a virtual environment. After updating the source, run `pip install .` again.

## Using a configuration file

A configuration file is optional. Create one only to change the defaults, for example the Codex path or the concurrency limit. See [configuration](configuration.md). Pass it in the client command:

```bash
claude mcp add --transport stdio --scope user codex -- python3 "$PWD/server.py" --config /absolute/path/config.toml serve
```

Use the same `--config` for `status`, `stop` and `review`.

## Windows notes

- Use the `py` launcher (`py -3`) if `python` is not on `PATH`.
- If `codex` was installed with npm, the bridge automatically finds the native `codex.exe` behind the `codex.cmd` launcher. If that fails, set `codex_binary` to the full path of `codex.exe` in a configuration file.
- Codex's legacy **unelevated** Windows sandbox cannot block reads outside the project, so the bridge refuses to use it. Use the default sandbox that Codex sets up on first run. See OpenAI's [Windows sandbox guide](https://learn.chatgpt.com/docs/windows/windows-sandbox).
- Communication uses a private named pipe that only your Windows account can open. No network port is used.
- State is stored in `%LOCALAPPDATA%\CodexMCP`. WSL is not required.

## Running the broker yourself

Some MCP hosts kill every child process when they exit, which also kills the background broker. If tasks stop whenever you close the client, start the broker from a normal terminal first:

```bash
python3 server.py start
```

The MCP server then connects to that broker instead of starting its own.

## Verifying an installation

```bash
python3 server.py --version     # codex-local-mcp 0.1.1
python3 server.py status        # "signed_in": true, codex_version 0.167.0+
python3 server.py call codex_models --args '{"limit": 3}'
```

None of these runs a model. To run the offline test suite, which needs no Codex sign-in, see [development](development.md).

## Updating

```bash
cd codex-mcp
git pull                  # or download the new ZIP / "Fetch origin" + "Pull" in GitHub Desktop
python3 server.py stop    # stop the old broker
```

Restart your MCP client afterwards. Saved tasks and history are kept. If you skip `stop`, tool calls fail with *"A broker started by a different version or configuration is still running"* until you stop it.

## Uninstalling

1. `python3 server.py stop`
2. Remove the server from your client (`claude mcp remove codex --scope user`, or delete it from the JSON configuration).
3. If you installed the package: `python3 -m pip uninstall codex-local-mcp`.
4. Delete the `codex-mcp` folder.
5. Optionally delete the state folder, which holds task history, prompts and output: `~/.local/state/codex-mcp` (or `$XDG_STATE_HOME/codex-mcp`) on macOS/Linux, `%LOCALAPPDATA%\CodexMCP` on Windows.

Codex's own conversation history (in `~/.codex`) is managed by Codex and is not removed.
