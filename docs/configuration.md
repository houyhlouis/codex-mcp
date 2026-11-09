# Configuration

English | [简体中文](configuration.zh-CN.md) · [Back to README](../README.md)

**No configuration file is needed.** Projects are chosen by the main agent at run time with `codex_projects` or a `workdir` argument. There is no list of allowed workspaces to maintain.

Create a settings file only to change one of the defaults below.

## Settings

| Setting | Default | Description |
|---|---|---|
| `codex_binary` | `"codex"` (looked up on `PATH`) | Codex executable. Use an absolute path if your MCP client does not see the same `PATH` as your terminal. A bare name is only looked up on `PATH`, never in the current folder |
| `state_dir` | `~/.local/state/codex-mcp` (macOS/Linux, or `$XDG_STATE_HOME/codex-mcp`); `%LOCALAPPDATA%\CodexMCP` (Windows) | Private folder for the broker's database, log and socket. Must be an absolute path of its own, not your home folder. On macOS/Linux the full socket path must fit in 100 bytes |
| `max_tasks` | `4` | How many independent task families may run at the same time (1–32) |
| `toolchain_paths` | `[]` | Extra **read-only** absolute paths that Codex's sandbox may use, such as a toolchain installed in an unusual place. System runtimes and the Codex executable are already readable |
| `approval_relay` | `false` | If `true`, the main agent may *approve* requests on your behalf. Leave it off unless you fully trust the main agent. Not needed for *Approve for me* |
| `allow_external_tools` | `false` | If `true`, Codex tasks may use the external MCP servers and plugins already configured in your Codex setup. These run outside the project sandbox |

Example (see [config.example.toml](../config.example.toml)):

```toml
# ~/.config/codex-mcp/config.toml
codex_binary = "/usr/local/bin/codex"
max_tasks = 2
toolchain_paths = ["/opt/toolchains/node-22"]
```

On Windows, use TOML single-quoted strings so that backslashes need no escaping:

```toml
codex_binary = 'C:\Tools\codex\codex.exe'
```

Paths may start with `~`. Environment variables such as `$HOME` or `%USERPROFILE%` are **not** expanded.

## Using the file

Pass `--config` **before** the command, everywhere you run the server:

```bash
python3 server.py --config ~/.config/codex-mcp/config.toml serve      # in the MCP client configuration
python3 server.py --config ~/.config/codex-mcp/config.toml status
python3 server.py --config ~/.config/codex-mcp/config.toml stop
```

After changing the file, run `stop` and restart your MCP client. A running broker refuses clients that use a different configuration.

## Rules for the settings file

- It must be owned by you and not writable by other users. On macOS/Linux, run `chmod 600 config.toml`.
- Keep it outside any project folder that you give to Codex. Codex is denied access to the settings file and the state folder.
- Unknown keys are rejected, so typos are reported instead of silently ignored.

## Upgrading from early development versions

The old `[workspace_roots]` section has been removed. Delete it from your settings file, then run `stop`. Use `workdir` or `project_id` instead of the old `root` and relative `path` arguments. Existing tasks are migrated automatically and their directories are checked again before use.
