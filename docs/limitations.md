# Limitations

[Back to README](../README.md)

These are deliberate design boundaries or current gaps, with the recommended workaround.

| Limitation | Why | What to do instead |
|---|---|---|
| The main agent must poll for progress | MCP clients differ in how (or whether) they handle notifications | Poll `codex_events` with `wait_seconds` up to 25. Pending requests are included in every page |
| Human approvals need a client prompt or a terminal | A tool argument claiming "the human approved" is not proof | Use a client with MCP form elicitation, `server.py review REQUEST_ID`, *Approve for me*, or (if you trust the main agent) `approval_relay = true` |
| Only one-shot approvals | Session approvals and rule amendments would expand future authority | Approve individual requests; change long-term policy in Codex directly |
| No secret answers, no advanced forms | Secrets in MCP arguments end up in model transcripts; the small form validator rejects complex schemas | Use Codex's own sign-in or the provider's browser flow |
| No exactly-once execution across a lost acknowledgement | Codex may accept an action just before the connection drops | The operation is marked `uncertain`; inspect `codex_get`/`codex_history` before acting again |
| Stream deltas can be missed during a crash | The app-server has no replay cursor for live events | Committed events survive; resume and page `codex_history` |
| No raw shell, PTY or arbitrary Codex RPC | These run outside the sandbox or bypass the project boundary | Ask Codex to run commands through its normal sandboxed tools; use the Codex terminal for interactive programs |
| No login/logout, global config, plugin or hook management | These affect more than the selected project | Manage them in Codex directly |
| No automatic override of an automatic-review denial | Would undermine the reviewer | Review the denial in Codex yourself |
| No Git worktree management | Git operations can run hooks and modify shared repository data | Create worktrees yourself and select each as its own project |
| Toolchains in unusual locations may be unreadable in the sandbox | The sandbox allows only minimal system paths | Add specific read-only paths to `toolchain_paths` |
| Only Codex sessions from the exact project can be imported | Other threads would bypass ownership checks | Import with `codex_sessions` from the matching project after stopping them elsewhere |
| A cancelled subagent approval can leave the parent waiting | Native Codex behaviour | Interrupt the parent's turn and send a follow-up |
| An idle task may run a queued message immediately | Native queue behaviour | Check status before updating or re-adding queue entries |
| MCP 2026-only clients | The server implements protocol versions 2024-11-05 to 2025-11-25 | Clients with a compatibility fallback (such as the official MCP SDK 1.26) work |
| No remote access | Local-only by design | Run the server on the same machine as the MCP client |

Unknown request types from Codex, including external token-refresh and attestation callbacks, receive an error and grant nothing.

## Testing coverage

The offline test suite runs on Windows, macOS and Linux in CI. Live tests against a real Codex account have been run on Linux; see [development](development.md#validation-status). Claude Code's user interface and live use on native Windows and macOS have not been verified by the maintainers' live tests.
