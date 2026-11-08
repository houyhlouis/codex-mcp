# Troubleshooting

English | [简体中文](troubleshooting.zh-CN.md)

Run the commands below from the source root. Use py -3 on Windows if python is not on PATH. Replace configuration paths if you chose a different location.

## Check startup

```bash
python3 --version
codex --version
codex login status
python3 server.py --config "$HOME/.config/codex-mcp/config.toml" status
```

`status` starts the private broker/native connection when needed, but does not start a coding turn. It reports authentication type without exposing credentials.

| Symptom | Explanation and next step |
|---|---|
| `No module named fcntl` or missing Unix APIs | Use the current source, which has native Windows adapters; see [platform notes](platforms.md). |
| `Codex executable not found` | Set `codex_binary` to the installed executable's absolute path, or make it available in the MCP client's `PATH`. |
| Work directory does not exist | Ask the main agent to create it with codex_projects, or select an existing absolute workdir. |
| Project overlaps private state/configuration | Choose a dedicated project directory that does not contain private server state or settings. The whole home directory is not accepted. |
| Configuration ownership/mode error | Use a file owned by the current Linux user and remove group/other write access, for example `chmod 600` on that configuration file. |
| State directory permission error | An existing state directory must be owned by you and mode `0700`. Choose a dedicated private path; do not relax checks. |
| Unix socket path too long | Set a shorter private `state_dir`. The implementation requires the complete socket path to fit within 100 bytes. |
| Broker failed to start | Inspect `broker.log` in the configured private state directory. Restricted containers may prohibit Unix sockets or required sandbox facilities. |
| Native sandbox startup fails | Check that normal Codex works with its OS sandbox in this environment. The bridge does not substitute full access. |
| Custom runtime/tool missing inside Codex | Add only the necessary read-only runtime/dependency path to `toolchain_paths`, then restart the broker. |
| Model, effort or tier rejected | Refresh `codex_models` and use offered combinations. Catalog visibility does not guarantee account entitlement. |
| A named permission profile is rejected | Inspect the native error and applicable administrator requirements. Do not downgrade to unrestricted access to bypass it. |

## After changing configuration or upgrading source

Stop the broker and reconnect the MCP client:

```bash
python3 server.py --config "$HOME/.config/codex-mcp/config.toml" stop
```

The next tool call starts it again. Startup matches the configuration and server-version fingerprint, so a new adapter will not silently reuse a differently configured broker. Remove legacy workspace_roots settings and select projects through the tools. Resume saved conversations and inspect history before continuing unfinished work.

## A task is waiting

- Read `codex_requests` or `codex_get.pending_requests`, including the originating child task ID.
- Answer `parent_question` with `codex_respond` using `response: {"text": "..."}`.
- Use `codex_review_request` for a human prompt. If the client cannot display one, run `review REQUEST_ID` in a local interactive terminal.
- `approve_for_me` is native automatic review, not the separate `approval_relay` setting. It can still deny actions or leave requests needing human input.
- Expired requests cannot be answered after a backend restart. Resume/reconcile and obtain a fresh request.

Cancelling a child approval can leave its parent's native wait pending. Interrupt the parent's current turn, wait for the interrupted state, and send a follow-up explaining what to do. Do not retry the denied action automatically. See [examples](../examples/native-controls.md).

## Queues, retries and incomplete progress

An idle loaded conversation can consume a queued message immediately. If update/delete reports that the message is no longer pending, inspect current state and history rather than adding it again.

Reuse an operation key only with identical arguments. An uncertain acknowledgement means the upstream action may already have happened; inspect saved tasks and native history before deciding on another operation. A saved successful acknowledgement can be stale, so use `codex_get` for the latest state.

Keep event cursors only after consuming each page. Use separate cursors for different task filters and process `has_more` pages before waiting. Clients can truncate large tool output; request smaller event/history pages if necessary. No client notification is required for polling, but the main agent must continue polling to discover questions and completion.

## Preparing a useful issue report

Include the server version, Python/Codex versions, operating system, failing tool/action, redacted error, and whether it reproduces with the offline fixture tests. Provide a minimal configuration with placeholder paths. Avoid uploading a whole state database, native conversation history or auth directory: those can contain private prompts, output and project data. Use [SECURITY.md](../SECURITY.md) for security-sensitive reports.
