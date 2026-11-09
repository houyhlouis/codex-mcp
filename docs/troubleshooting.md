# Troubleshooting

English | [简体中文](troubleshooting.zh-CN.md) · [Back to README](../README.md)

Run the commands from the `codex-mcp` folder. On Windows, use `py -3` instead of `python3`. If you use a settings file, add `--config /path/config.toml` before the command.

## First checks

```bash
python3 --version              # 3.11 or newer
codex --version                # 0.167.0 or newer
codex login status             # signed in
python3 server.py --version
python3 server.py status       # starts the broker; should show "signed_in": true
```

`codex doctor` checks the Codex installation itself. The broker's private log is `broker.log` in the state folder: `~/.local/state/codex-mcp/` on macOS/Linux, `%LOCALAPPDATA%\CodexMCP\` on Windows.

## Common problems

| Symptom | Cause and fix |
|---|---|
| `Codex executable not found` | Codex is not on the `PATH` that the MCP client sees. Set `codex_binary` to the absolute path in a [settings file](configuration.md). |
| `Codex 0.167.0 or newer is required` | Run `codex update`, then `python3 server.py stop`. |
| `codex` is missing or failed in `/mcp` / the client's server list | Run the exact command from your client configuration in a terminal (with `status` instead of `serve`) and read the error. Check that the path to `server.py` is absolute. |
| `A broker started by a different version or configuration is still running` | You updated the code or changed settings. Run `python3 server.py stop` (with the same `--config`, if any) and retry. |
| `Broker failed to start` / `did not become ready` | Read `broker.log` in the state folder. Some locked-down containers forbid Unix sockets. |
| `Work directory ... missing` / `must be an absolute path` | Pass an absolute path, or let the main agent create the folder with `codex_projects` (`action: "create"`). |
| `Select a dedicated project directory, not the home directory` | Choose a project subfolder. Your whole home folder or a drive root cannot be a project. |
| `Project directory overlaps private state` | The project contains, or is inside, the state folder or the settings file. Use another folder. |
| `Another task is active in this project` | Only one independent task may work in a folder at a time. Wait, interrupt it, or use a separate folder or worktree. |
| `Concurrent task limit reached` | Finish or interrupt a task, or raise `max_tasks`. |
| `Configuration must be owned by this user...` | Run `chmod 600` on the settings file (macOS/Linux). On Windows, make sure only your account can modify it. |
| `State directory must be owned by this user with mode 0700` | `chmod 700` the state folder, or choose a new `state_dir`. |
| `state_dir is too long for a Unix socket path` | Choose a shorter `state_dir`. |
| `The legacy unelevated Windows sandbox cannot enforce denied reads` | Switch Codex to its default Windows sandbox (see [installation](installation.md#windows-notes)). |
| A tool inside Codex is "not found" (for example a compiler in an unusual location) | Add its folder to `toolchain_paths`, then `stop` the broker. |
| `Model is not in the visible Codex catalog` / `Unsupported reasoning effort` | Call `codex_models` and use a listed combination. |
| `Previous operation with this request_id is uncertain` | That key was already used and failed or was interrupted. Inspect the task, then use a new `request_id`. |
| `request_id was already used with different arguments` | Use a new, unique `request_id` for each new action. |

## A task is waiting

A `waiting` task needs an answer.

1. List pending requests with `codex_requests` (or read `pending_requests` from `codex_get`).
2. For a `parent_question`, the main agent answers with `codex_respond` and `{"text": "..."}`.
3. For approvals and user questions, use `codex_review_request`. If your client cannot show a prompt, run `python3 server.py review REQUEST_ID` in a terminal.
4. Requests expire when Codex restarts. If an answer is rejected as expired, call `codex_resume` and let Codex ask again.

If cancelling a subagent's approval leaves its parent waiting, interrupt the parent's turn, wait for `interrupted`, and send a follow-up that explains what to do instead.

## Tasks stop when the client closes

Some MCP hosts kill all child processes on exit, which also kills the broker. Start the broker from a normal terminal with `python3 server.py start` before opening the client.

## Recovering after a crash or restart

- Active tasks become `needs_resume`. Call `codex_resume`, read `codex_history`, then send a follow-up. Nothing is replayed automatically.
- Keep reading `codex_events` from your last saved cursor; committed events are never lost.
- An `uncertain` status means an action's acknowledgement was lost. Check `codex_get` and `codex_history` before repeating anything.
- If a queued message "is no longer pending", Codex has probably already started it. Check the task instead of adding it again.

## Reporting a problem

Include the output of `python3 server.py --version`, `codex --version`, your operating system, the tool call that failed and the (redacted) error message. Say whether the offline tests (`python3 -m unittest discover`) pass. Do not upload the state folder, `~/.codex` or full conversation history; they can contain private code and prompts. For security issues, see [SECURITY.md](../SECURITY.md).
