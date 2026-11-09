# Using the Codex subagent

Use the `codex` MCP server for coding tasks that benefit from a persistent Codex conversation.

## Starting work

- Pick or create the project folder with `codex_projects`, then call `codex_start` with its `project_id` (or an absolute `workdir`).
- Give Codex a concrete goal, the expected result, relevant constraints and how to test it. Never include passwords, tokens or keys.
- Use only model/effort combinations listed by `codex_models`.
- Use a new, unique `request_id` for every action. Reuse a `request_id` only to retry the identical call after a lost reply.
- Save the returned `task_id`, and use it again for related follow-ups.

## While Codex works

- Poll `codex_events` with the last processed `cursor` and `wait_seconds` up to 25. Fetch again immediately while `has_more` is true. Handle every pending request in each page.
- Answer `parent_question` requests (`ask_parent`) yourself with `codex_respond` and `{"text": "..."}`.
- Show approvals and user questions to the human with `codex_review_request`. If the client cannot show a prompt, ask the human to run `python3 server.py review REQUEST_ID`. Never invent consent. You may deny a request yourself.
- Steering and interrupting need the current `turn_id`. After interrupting, wait for the `interrupted` status.
- Do not run two independent tasks in the same or overlapping folders. Use separate worktrees for parallel work.

## Finishing

- Report the actual status, changed files, commands with their exit codes, test results, failures and remaining work. An `interrupted` or `failed` turn is not a success.
- Check `family_status` and subagent statuses before treating delegated work as done.

## Native features

- *Approve for me*: `approval_mode: "approve_for_me"` on `codex_start` or `codex_settings`. It uses Codex's own reviewer; respect its denials.
- Subagents: ask the parent with `codex_delegate`, find children with `codex_subagents`. If a child cannot accept direct input, instruct its parent instead.
- Plan mode, model changes, forks, code review, compaction and queues are available as tools. A queued message may run immediately; check the state before editing or re-adding it.
- Set or change persistent goals (`codex_goal`) only when the user explicitly asks. Import a CLI session with `codex_sessions` only after the user confirms it is stopped elsewhere.

## Recovery

- If a reply is lost, retry with the same `request_id` and arguments.
- If a task is `needs_resume` or `uncertain`, call `codex_resume`, inspect `codex_history`, and only then decide on new work.
