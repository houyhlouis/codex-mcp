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
- [What has been verified](#what-has-been-verified)
- [Backing up and cleaning up](#backing-up-and-cleaning-up)
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
| `server.py doctor` | Check the installation without starting anything. See [troubleshooting](troubleshooting.md#run-the-doctor-first). |
| `server.py backup` | Save a copy of the state database. See [backing up and cleaning up](#backing-up-and-cleaning-up). |
| `server.py prune` | Remove old data from the state database, after showing a plan. |
| `server.py incident-export` | Write the structure of a task, without its content, for a bug report. See [reporting a problem](troubleshooting.md#reporting-a-problem). |
| `server.py worktree` | Prepare, inspect, hand off or remove a Git worktree from a terminal; the same helper as [`codex_worktrees`](tools.md#worktrees). Use it while no Codex task works in the main checkout. |
| `server.py call TOOL --args JSON` | Call one of the 34 tools directly, for diagnosis. |
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

A client works if it can start a **stdio** server and speaks one of the MCP versions listed below. Configure:

- **command:** `python3` (Windows: `py`)
- **arguments:** `/absolute/path/to/codex-mcp/server.py serve` (Windows: `-3 C:\path\to\codex-mcp\server.py serve`)

**Supported MCP versions:** 2024-11-05, 2025-03-26, 2025-06-18, 2025-11-25 and 2026-07-28. On 2026-07-28 only the stdio transport is supported, and the optional Tasks extension is available; see the [tool reference](tools.md#mcp-2026-07-28-and-tasks).

**Approval prompts** appear inside the client only if it supports *form elicitation* (MCP 2025-06-18 or newer). With any other client, approve requests in a terminal with `python3 server.py review REQUEST_ID`. Everything else works the same.

**What has been tested** is listed under [What has been verified](#what-has-been-verified).

## Installing as a Python package (optional)

Running `server.py` from the folder needs no installation. If you prefer a command on your `PATH`, install the package from the downloaded folder:

```bash
cd codex-mcp
python3 -m pip install .
codex-local-mcp --version
```

Then use `codex-local-mcp serve` in your client configuration, for example `claude mcp add --transport stdio --scope user codex -- codex-local-mcp serve`. On Linux distributions that block system-wide `pip`, use `pipx install .` or a virtual environment. After updating the source, run `pip install .` again.

Note the three names: the repository is `codex-mcp`, the package and its command are `codex-local-mcp`, and the Python module is `codex_local_mcp`. The package is not on PyPI, so `pip install codex-local-mcp` does not work. Always install from the folder.

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

### WSL

Codex MCP runs either completely on Windows or completely inside WSL. The server, Python, the Codex CLI and the project folder must be in the same environment, because the sandbox and the path checks belong to the system that Codex runs on.

- **Everything in WSL, and the client too** (for example Claude Code started from a WSL shell): follow the Linux instructions.
- **Everything in WSL, but the client on Windows** (for example Claude Desktop): let the client start the server through `wsl.exe`:

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

  A ready-made file is [examples/mcp.wsl.json](../examples/mcp.wsl.json). Give project folders as Linux paths, such as `/home/me/projects/demo`. Keep projects in the Linux file system rather than under `/mnt/c`, where file permissions work differently. Run `server.py review`, `status`, `stop` and `doctor` inside WSL as well.
- **Do not mix the two.** A server on Windows cannot use a Codex that is installed in WSL, and the other way round. If `doctor` passes in your terminal but the client fails, check in which environment the client starts the server.

The WSL setups follow from how the server works. They have not been tried by the maintainers.

## Running the broker yourself

Some MCP hosts kill every child process when they exit, which also kills the background broker. If tasks stop whenever you close the client, start the broker from a normal terminal first:

```bash
python3 server.py start
```

The MCP server then connects to that broker instead of starting its own.

## Verifying an installation

```bash
python3 server.py --version     # codex-local-mcp 0.2.0
python3 server.py status        # "signed_in": true, codex_version 0.167.0+
python3 server.py call codex_models --args '{"limit": 3}'
```

None of these runs a model. To run the offline test suite, which needs no Codex sign-in, see [development](development.md).

For a check that starts nothing, run `python3 server.py doctor`; see [troubleshooting](troubleshooting.md#run-the-doctor-first).

The output of `status` also contains `compatibility`. `tested` means that your Codex version is one the project is tested with. `untested` means a prerelease or a newer version, which normally works but has not been checked.

## What has been verified

"Works" can mean different things, so each line below says how it was checked.

| Level | Meaning |
|---|---|
| **Protocol** | The messages are checked automatically against the MCP specification or Codex's own schema, with a simulated Codex |
| **Platform** | The offline test suite ran on that operating system and Python version |
| **Live** | Run against a real Codex account |
| **Client** | Used through the real user interface of that client |

| Combination | Checked at level | Notes |
|---|---|---|
| MCP 2024-11-05, 2025-03-26, 2025-06-18, 2025-11-25 | Protocol | Tested over stdio. The official MCP Python SDK 1.26.0 also runs against the server in CI |
| MCP 2026-07-28 with Tasks | Protocol | Checked against the published specification and schema only. No SDK and no real client yet |
| Codex CLI 0.167.0 | Protocol, Live | The server's messages are validated against the schema that Codex generates. Older versions are refused; newer ones are reported as `untested` |
| Linux, Python 3.11 and 3.14 | Platform, Live | Live tests used a ChatGPT sign-in |
| Windows and macOS, Python 3.11 and 3.14 | Platform | Run by CI on every push. v0.1.1 passed on both. Version 0.2.0 has not run there yet |
| Claude Code on Linux | Client | Used with v0.1.1 for long sessions: starting tasks, follow-ups, answering Codex's questions, *Approve for me*, resuming after a restart. Its approval prompt has not been used |
| Claude Desktop and other clients | not verified | Expected to work, because they use the same protocol |
| WSL | not verified | See [WSL](#wsl) |
| Sign-in with an API key | not verified | |

A protocol check shows that the server speaks correctly. It does not show how a particular client displays a prompt or handles a long wait. The dated results behind this table are in [validation status](development.md#validation-status).

## Backing up and cleaning up

The state folder grows with every task, and nothing in it is removed automatically. Two commands let you manage it. Neither is available to the main agent; you run them yourself in a terminal.

### Backup

```bash
python3 server.py backup
```

This writes a copy of the database into the state folder, named `state.sqlite3.backup-<number>.bak`, checks the copy and prints its path. It works while the broker is running and does not disturb active tasks.

To write the copy somewhere else, add `--destination /absolute/path/file.bak`. The folder must be private to your account and must not be inside a project. An existing file is never overwritten. Backups made this way are kept until you delete them. They contain your prompts and Codex's output, so treat them like the state folder.

### Cleaning up old data

Cleaning up takes two steps, so that you see what would be removed before anything is removed.

1. Stop the broker, then ask for a plan:

   ```bash
   python3 server.py stop
   python3 server.py prune --older-than 30d
   ```

   The plan lists how much would be removed, how much is protected and why, and a plan ID. Nothing has changed yet. `--older-than` accepts `s`, `m`, `h`, `d` and `w`; a number without a unit means days. Add `--max-bytes N` to remove only as much old data as is needed to get the stored data below `N` bytes.

2. Apply the plan:

   ```bash
   python3 server.py prune --apply --plan-id PLAN_ID
   ```

   The data is checked again first. If anything changed since the plan was made, or the plan is older than one hour, it is refused and you make a new one. Up to 32 plans can wait at the same time; old ones are removed by themselves. Add `--compact` to shrink the database file afterwards; without it, the freed space is reused by new data.

`prune` refuses to run while a broker is running and never stops one itself. Both commands accept `--json`.

**What is removed:** old events, saved results of finished operations, closed questions and approvals, turn results and their artifacts, delegation contracts of finished tasks, records of expired MCP tasks, and attached images that were never sent to Codex.

**What is never removed:**

- anything that belongs to a task that is still running, waiting or queued, or to a task with an open question or approval;
- anything a project reservation or an operation with an unknown outcome still depends on;
- the list of tasks and projects itself;
- images that were sent to Codex, because Codex's own history may still refer to them;
- the record that an operation key was used. A key whose result was removed stays blocked, so that repeating the call can never run the work twice;
- Codex's own history in `~/.codex`, and every file in your projects.

After a cleanup, an old event cursor or an old operation key gets a clear error (`cursor_expired`, `operation_expired`) instead of wrong data. See the [tool reference](tools.md#error-codes).

**Right after an update from 0.1.x,** the plan may say that some tasks have not been re-checked since the upgrade, and protect everything. Start the server once (`python3 server.py start`), wait a moment while it checks the old tasks with Codex, stop it, and make a new plan.

## Updating

```bash
cd codex-mcp
python3 server.py stop    # stop the old broker first
git pull                  # or download the new ZIP / "Fetch origin" + "Pull" in GitHub Desktop
```

Stop the broker before you replace the files, so that the old broker never loads a file from the new version, and do it when no task is working. `stop` ends the turns that are running, but not necessarily the commands they started; a task that was working keeps its project reserved afterwards until you call `codex_resume` for it (see [Commands that keep running](troubleshooting.md#commands-that-keep-running)).

Restart your MCP client afterwards. Saved tasks and history are kept. If you skip `stop`, tool calls fail with *"A broker started by a different version or configuration is still running"* until you stop it.

## Uninstalling

1. `python3 server.py stop`
2. Remove the server from your client (`claude mcp remove codex --scope user`, or delete it from the JSON configuration).
3. If you installed the package: `python3 -m pip uninstall codex-local-mcp`.
4. Delete the `codex-mcp` folder.
5. Optionally delete the state folder, which holds task history, prompts and output: `~/.local/state/codex-mcp` (or `$XDG_STATE_HOME/codex-mcp`) on macOS/Linux, `%LOCALAPPDATA%\CodexMCP` on Windows.

Codex's own conversation history (in `~/.codex`) is managed by Codex and is not removed.
