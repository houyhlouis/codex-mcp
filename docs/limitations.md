# Limitations

[Back to README](../README.md)

These are deliberate design boundaries or current gaps, with the recommended workaround.

| Limitation | Why | What to do instead |
|---|---|---|
| The main agent must poll for progress | MCP clients differ in how (or whether) they handle notifications | Poll `codex_events` with `wait_seconds` up to 25. Pending requests are included in every page |
| Human approvals need a client prompt or a terminal | A tool argument claiming "the human approved" is not proof | Use a client with MCP form elicitation, `server.py review REQUEST_ID`, *Approve for me*, or (if you trust the main agent) `approval_relay = true` |
| Only one-shot approvals | Session approvals and rule amendments would expand future authority | Approve individual requests; change long-term policy in Codex directly |
| No secret answers, no advanced forms | Secrets in MCP arguments end up in model transcripts; the small form validator rejects complex schemas | Use Codex's own sign-in or the provider's browser flow |
| Running commands are known only from Codex's reports, plus a best-effort check on Linux | With Codex 0.167.0 on Linux, a command can keep running after Codex reported it as terminated, after a broker stop, or when it detached itself (`nohup ... &`). On Linux the server finds such leftovers as long as they are still inside Codex's sandbox process, and keeps the project reserved. It recognises that process by its name, which a future Codex version may change. On macOS and Windows there is no such check | Before you start an independent task in a folder where background commands ran, check the process list on macOS and Windows. See [Leftover processes on Linux](tools.md#leftover-processes-on-linux) and [Troubleshooting](troubleshooting.md#commands-that-keep-running). After a restart the server keeps such a project reserved until `codex_resume` |
| No exactly-once execution across a lost acknowledgement | Codex may accept an action just before the connection drops | The operation is marked `uncertain`; inspect `codex_get`/`codex_history` before acting again |
| Stream deltas can be missed during a crash | The app-server has no replay cursor for live events | Committed events survive; resume and page `codex_history` |
| No raw shell, PTY or arbitrary Codex RPC | These run outside the sandbox or bypass the project boundary | Ask Codex to run commands through its normal sandboxed tools; use the Codex terminal for interactive programs |
| No login/logout, global config, plugin or hook management | These affect more than the selected project | Manage them in Codex directly |
| No automatic override of an automatic-review denial | Would undermine the reviewer | Review the denial in Codex yourself |
| Codex cannot use Git inside a managed worktree, and some repositories cannot get one | A worktree's Git data lies outside its project, and Codex offers no per-task read-only access to it. The server's own Git refuses repositories that could make it run commands (filters such as Git LFS, included configuration, program settings) | Use the handoff patch as the result, or prepare such a worktree by hand and select it as its own project. See [Worktrees](tools.md#worktrees) |
| Budgets are cooperative and cannot be exact | Usage arrives from Codex in pieces and with delay; turns Codex starts for a native goal cannot be prevented | Soft limits may overshoot; read `budget_stop` and `family_usage.gaps`. See [Family usage and budgets](tools.md#family-usage-and-budgets) |
| Plans are small and never retry | Up to 16 steps, at most 4 at once; a step with an unknown outcome waits for `codex_resume`, and nothing is started twice. When the server cannot tell which Codex turn belonged to a step, the step stays `uncertain` | Split larger workflows; resolve `uncertain` steps by hand, or cancel the plan and delete the step's task. See [Plans](tools.md#plans) |
| A task stopped before Codex opened its conversation can leave an empty conversation in Codex | Codex may have opened it just before the server stopped, and the server never learned its ID | Harmless; `codex_resume` ends the task as `failed`. See [A task that never reached Codex](tools.md#a-task-that-never-reached-codex) |
| A restored backup knows only what happened up to the moment it was taken | Work that started later is not in it | After restoring, check running tasks in Codex before starting new work |
| Toolchains in unusual locations may be unreadable in the sandbox | The sandbox allows only minimal system paths | Add specific read-only paths to `toolchain_paths` |
| Only Codex sessions from the exact project can be imported | Other threads would bypass ownership checks | Import with `codex_sessions` from the matching project after stopping them elsewhere |
| A cancelled subagent approval can leave the parent waiting | Native Codex behaviour | Interrupt the parent's turn and send a follow-up |
| An idle task may run a queued message immediately | Native queue behaviour | Check status before updating or re-adding queue entries |
| MCP 2026-07-28 over HTTP; subscriptions, resources, prompts and sampling | The server is local and offers tools only. On 2026-07-28 it supports stdio and the Tasks methods get, update and cancel | Use a stdio client. Everything is also available through the ordinary tools |
| No remote access | Local-only by design | Run the server on the same machine as the MCP client |

Unknown request types from Codex, including external token-refresh and attestation callbacks, receive an error and grant nothing.

## Testing coverage

The offline test suite runs on Windows, macOS and Linux in CI. Live tests against a real Codex account have been run on Linux; see [development](development.md#validation-status). Claude Code's user interface and live use on native Windows and macOS have not been verified by the maintainers' live tests.
