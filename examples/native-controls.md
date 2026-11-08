# Native Codex control examples

These are MCP tool arguments. The main agent chooses or creates the project directory. Substitute an actual absolute workdir or a project_id returned by codex_projects. Replace task/request/turn IDs with actual values. Do not send placeholder IDs literally. Tool schemas are available through MCP `tools/list`.

## Start with automatic review

Call `codex_start`:

```json
{"workdir":"/absolute/path/to/project","prompt":"Implement the requested change and run relevant tests.","approval_mode":"approve_for_me","request_id":"implementation-001"}
```

This uses native automatic review with restricted project access. `approval_relay` can remain false. Poll `codex_events` and check `last_automatic_review` in `codex_get`; a denied action remains denied unless Codex proceeds through an allowed alternative.

## Native delegation and child controls

On an idle parent, call `codex_delegate`:

```json
{"task_id":"PARENT_TASK","prompt":"Use one native subagent to inspect the parser and another to inspect its tests. Have them report findings, then summarize. Do not edit files yet.","request_id":"delegate-001"}
```

For an active parent, also pass its current `expected_turn_id`. Use `codex_subagents` with `task_id: "PARENT_TASK"` to discover children. Parent `codex_events` polling includes child progress and requests, so no separate notification UI is required. Each child has its own task ID for history/status/interruption.

If a child reports `can_accept_direct_input: false`, call `codex_delegate` on the parent with instructions to message, continue or close that particular child. Include its native thread ID, nickname or `native_agent_path`. The parent uses the native agent controls rather than the bridge sending an unsupported direct turn.

If cancelling a child approval leaves the parent waiting, use `codex_interrupt` on the parent's active turn, wait for `interrupted`, then `codex_send` a follow-up describing the denied child action and what to do next. Do not restart the denied action automatically. This native wait edge case was observed in the live test.

## Plan mode, model and reviewer changes

Call `codex_settings`:

```json
{"task_id":"TASK","scope":"next","collaboration_mode":"plan","approval_mode":"ask","request_id":"plan-settings-001"}
```

Then use `codex_send` for the planning prompt. Return to `collaboration_mode: "default"` when implementation should begin. Model/effort and service tiers must come from `codex_models`.

To change the reviewer for a currently running turn:

```json
{"task_id":"TASK","scope":"current","expected_turn_id":"TURN","approval_mode":"approve_for_me","request_id":"current-reviewer-001"}
```

Native already-captured steps and pending approvals keep their existing reviewer; children already spawned keep their inherited settings.

## Fork and review

Call `codex_fork` on an idle family:

```json
{"task_id":"TASK","name":"Review branch","request_id":"fork-001"}
```

Call `codex_review` on the returned task:

```json
{"task_id":"FORK_TASK","target":"baseBranch","branch":"main","request_id":"review-001"}
```

Poll normal events for findings and completion. Forks retain history but do not create a Git worktree. Independent active forks in the same directory are prevented; select separate existing worktrees for parallel root tasks.

## Queued follow-ups

Call `codex_queue`:

```json
{"task_id":"TASK","action":"add","prompt":"After the current turn, document the change and rerun the relevant test.","request_id":"queued-followup-001"}
```

Use action `list` to inspect pending entries. `update` and `delete` take `queued_id`; `reorder` takes the complete `ordered_ids` list. `start` starts a pending entry on an idle task. **An idle loaded conversation may consume an added entry immediately**, so it might already be running before an update arrives. Inspect status/history instead of duplicating the instruction.

## Images, reference files and skills

Call `codex_send`:

```json
{"task_id":"TASK","prompt":"Use the specification and screenshot to propose a fix.","context_files":["docs/spec.txt"],"images":["test-data/screenshot.png"],"request_id":"context-001"}
```

Paths are relative to the approved project, not the main agent's current directory. For skills, first call `codex_skills`, then include enabled unique names in the `skills` array. `output_schema` can constrain a new turn's final output.

## Import an existing CLI conversation

First stop work in that native CLI conversation. Call `codex_sessions` with `workdir` or `project_id` and `action: "list"`, then explicitly import the selected native thread:

```json
{"workdir":"/absolute/path/to/project","action":"import","thread_id":"NATIVE_THREAD","confirm_stopped":true,"request_id":"import-001"}
```

Only a root conversation at that exact approved working directory can be imported. The bridge reapplies its restricted project profile before continuing. It cannot reliably detect activity in a separate app-server process; `confirm_stopped` records the caller's responsibility to stop other clients.

## History and lifecycle

`codex_compact` compacts an idle conversation. `codex_archive` supports archive/unarchive. `codex_revert` removes a selected turn and everything after it **without reverting files**, and requires `confirm_history_only: true`. `codex_delete` requires the exact native thread ID in `confirm_thread_id` and permanently removes that native conversation and its descendants. Bridge audit records remain.

`codex_goal` supports native get/set/clear. Mutations require an explicit user request, `user_requested: true`, and an operation key. Set a token budget only when the user requests one. Active goals can cause continuing work and account usage; an ordinary coding prompt does not authorize a persistent goal.
