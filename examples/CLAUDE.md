# Using the Codex subagent

Use the `codex` MCP server for coding tasks that benefit from a persistent Codex conversation.

- Discover projects and model options before starting. Choose only catalog-supported model/effort pairs.
- Provide a concrete task, project, expected result, relevant constraints and desired tests. Never include credentials.
- Save the returned task ID. Reuse the conversation for related follow-ups.
- Poll `codex_events` with the last consumed cursor and up to 25 seconds of waiting. Handle every page and all pending requests; notifications are optional.
- Answer `ask_parent` questions with task context. Native approvals and user questions should be presented to the human through `codex_review_request`; explain the terminal review option when the client cannot show a prompt. Do not manufacture consent.
- Steering and interruption must use the current turn ID. Wait for the resulting state before assuming work stopped.
- Do not run concurrent coding agents in overlapping directories. Use separate existing worktrees when independent work is needed.
- If a control response is lost, retry with the same operation key and arguments. If its outcome is uncertain, inspect tasks, events and persisted history before issuing new work.
- Report Codex's result with the actual task status, changed files, command/test evidence, failures, and remaining work. An interrupted or failed turn is not a successful completion.
- Native Approve for me is `approval_mode: "approve_for_me"` on start or `codex_settings`; it uses Codex's reviewer and does not require enabling the separate approval relay. Respect native denials.
- Use `codex_delegate` for explicit native delegation and `codex_subagents` to discover child task IDs. Parent polling includes child requests. If a child cannot accept direct input, instruct its parent to control it.
- Use native settings/plan mode, forks, code review, compaction, queued instructions and lifecycle tools as appropriate. Queued messages can run immediately; inspect state before retrying or editing a consumed queue entry.
- Create or modify persistent goals only when the user explicitly asks. Confirm stopped native sessions before importing them; never manufacture that confirmation while another CLI is still working.
