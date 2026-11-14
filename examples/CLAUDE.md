# Using the Codex subagent

Use the `codex` MCP server for coding tasks that benefit from a persistent Codex conversation.

## Starting work

- Pick or create the project folder with `codex_projects`, then call `codex_start` with its `project_id` (or an absolute `workdir`).
- Give Codex a concrete goal, the expected result, relevant constraints and how to test it. Never include passwords, tokens or keys.
- Use only model/effort combinations listed by `codex_models`.
- Use a new, unique `request_id` for every call that changes something. Reuse one only to retry the identical call after a lost reply. In `codex_respond` and `codex_review_request`, `request_id` is the ID of the pending request instead.
- Save the returned `task_id`, and use it again for related follow-ups.

## While Codex works

- Poll `codex_events` with the last processed `cursor` and `wait_seconds` up to 25. Fetch again immediately while `has_more` is true. Handle every pending request in each page.
- To watch several tasks, call `codex_events` once with `"view": "outcome"` and `task_ids`, and pass the returned `cursors` back in the next call.
- While you only wait, poll with `"view": "compact"` to save context. Read the full result once with `codex_get` when the turn has finished.
- Answer `parent_question` requests (`ask_parent`) yourself with `codex_respond` and `{"text": "..."}`. If the request has a `decision_summary` with options, add `"decision"` with its `id`, `context_hash` and your `option_id`.
- Show approvals and user questions to the human with `codex_review_request`. If the client cannot show a prompt, ask the human to run `python3 server.py review REQUEST_ID`. Never invent consent. You may deny a request yourself. A request marked `person_review_required` can only be accepted by the human.
- Steering and interrupting need the current `turn_id`. After interrupting, wait for the `interrupted` status.
- Do not run two independent tasks in the same or overlapping folders. Use separate worktrees for parallel work. If a project is reported busy after its turn ended, `codex_get` shows the reason in `workspace_claims`.

## Finishing

- Report the actual status, changed files, commands with their exit codes, test results, failures and remaining work. An `interrupted` or `failed` turn is not a success.
- Base the report on `codex_get` with `"view": "result"`. It separates what Codex claimed from the commands and changes it reported. If `evidence_completeness.complete` is false, say what is missing.
- Check `family_status` and subagent statuses before treating delegated work as done.

## Native features

- *Approve for me*: `approval_mode: "approve_for_me"` on `codex_start` or `codex_settings`. It uses Codex's own reviewer; respect its denials.
- Subagents: ask the parent with `codex_delegate`, find children with `codex_subagents`. If a child cannot accept direct input, instruct its parent instead.
- Plan mode, model changes, forks, code review, compaction and queues are available as tools. A queued message may run immediately; check the state before editing or re-adding it.
- Set or change persistent goals (`codex_goal`) only when the user explicitly asks. Import a CLI session with `codex_sessions` only after the user confirms it is stopped elsewhere.

## Recovery

- If a reply is lost, retry with the same `request_id` and arguments.
- A failed call returns `ok: false` and an `error`. Act on `error.code`. If `error.uncertain` is true, the action may already have happened: inspect the task with `codex_get` before doing anything else. Repeat a call unchanged only if `error.retryable` is true.
- If a task is `needs_resume` or `uncertain`, call `codex_resume`, inspect `codex_history`, and only then decide on new work. The result of `codex_resume` has a `reconciliation` report: read its `unknown` and `next_steps`. An acknowledged operation is not proof that the work was done.
- If a refusal or `process_state` says that commands from before a restart may still be running, tell the user and ask them to check the process list. Call `codex_resume` for that task only after they have answered. If a task's conversation no longer exists in Codex, tell the user before you remove the task with `codex_delete`.
- For parallel work on one repository, use `codex_worktrees` (when enabled) to give each task its own worktree, and return the result with `handoff`. Codex cannot run Git inside a worktree; do not ask it to commit there. Never remove a worktree the user has not reviewed.
- Respect `budget_exceeded` and `budget_stop`. Change a family budget only when the user asks.
- Use `codex_plans` only for a few clearly ordered steps the user has agreed to. Check `inspect` regularly; answer the steps' questions and approvals as for any task, and resolve an `uncertain` step with `codex_resume` before anything else. If it stays `uncertain`, read the task's history, stop the task, then cancel the plan and delete that task with `codex_delete`; never assume the step succeeded.
- If `codex_get` shows `evidence_complete: false`, the listed files and commands may be incomplete. Read `codex_history` before reporting results.
