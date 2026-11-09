# Usage guide

English | [简体中文](usage.zh-CN.md) · [Back to README](../README.md)

This guide explains how a main agent (for example Claude Code) works with Codex through this server. You normally just talk to your main agent in plain language, and it calls the tools for you. The JSON snippets show what happens underneath. Replace the `TASK`, `TURN` and `PENDING_ID` placeholders with real IDs.

- [The basic loop](#the-basic-loop)
- [Questions and approvals](#questions-and-approvals)
- [Follow-ups, steering and interrupting](#follow-ups-steering-and-interrupting)
- [Native subagents](#native-subagents)
- [Settings: model, plan mode, reviewer, access](#settings-model-plan-mode-reviewer-access)
- [Context files, images and skills](#context-files-images-and-skills)
- [Review, fork, queue and other controls](#review-fork-queue-and-other-controls)
- [After a restart or disconnect](#after-a-restart-or-disconnect)
- [Tips for good results](#tips-for-good-results)

## The basic loop

1. **Choose a project.** The main agent picks an absolute directory. `codex_projects` can create it and returns a `project_id`:

   ```json
   {"action": "create", "path": "/Users/me/projects/demo"}
   ```

   An existing directory can also be passed directly as `workdir`. The home directory itself, filesystem roots and the server's private folders are refused.

2. **Start a task.** `codex_start` returns a task immediately, while Codex works in the background:

   ```json
   {"project_id": "PROJECT_ID", "prompt": "Fix the failing parser tests and run them.", "request_id": "fix-parser-1"}
   ```

   `request_id` is a unique key for this action. If the reply is lost, repeating the call with the same key and arguments returns the saved result instead of starting a second task.

3. **Follow progress.** Call `codex_events` with the last cursor (start at `0`). `wait_seconds` (up to 25) makes the call wait for new events:

   ```json
   {"task_id": "TASK", "after": 0, "wait_seconds": 25}
   ```

   Process the page, save the returned `cursor`, and call again. If `has_more` is true, fetch the next page right away. Every page also lists `pending_requests`.

4. **Answer requests.** See [Questions and approvals](#questions-and-approvals).

5. **Read the result.** When the status is `completed` (or `failed` / `interrupted`), `codex_get` returns the summary, changed files, commands with exit codes, plan and token usage.

Each task is one persistent Codex conversation. Use the same `task_id` for related follow-up work.

## Questions and approvals

Pending requests appear in `codex_events`, `codex_get` and `codex_requests`. Each has a `kind`:

| Kind | What Codex wants | Who may answer |
|---|---|---|
| `parent_question` | Context or a decision from the main agent (`ask_parent`) | The main agent |
| `command_approval` | Permission to run a command outside its sandbox | A human; the main agent may only deny |
| `file_approval` | Permission to apply a patch | A human; the main agent may only deny |
| `permissions` | Extra permissions, such as network access, for this turn | A human; the main agent may only grant nothing |
| `user_input` | Answers to questions meant for the user | A human |
| `elicitation` | A form from an external MCP tool | A human; the main agent may decline |

**Answering a parent question** (`codex_respond`):

```json
{"request_id": "PENDING_ID", "response": {"text": "Use the existing parser interface and run its unit tests."}}
```

**Getting a human decision.** The main agent calls `codex_review_request`. If the client supports MCP form prompts, you see the request and choose. Nothing is approved if you dismiss the prompt or it times out. If the client has no prompt support, the request stays pending and you answer it in a terminal:

```bash
python3 server.py review PENDING_ID
```

**Denying** is always allowed without a human:

```json
{"request_id": "PENDING_ID", "response": {"decision": "decline"}}
```

Some commands offer only `accept` and `cancel`. Then use `cancel` to deny. For a `permissions` request, `{"permissions": {}, "scope": "turn"}` grants nothing.

**Approve for me.** Start the task with `"approval_mode": "approve_for_me"`, or switch with `codex_settings`. Codex's own automatic reviewer then decides eligible requests. It can approve or deny. The sandbox and project boundary stay in place. This is separate from the `approval_relay` setting, which you do not need to enable.

Silence never means approval. Requests expire when the turn ends or when the Codex connection restarts. An expired request cannot be answered; resume the task and let Codex ask again.

## Follow-ups, steering and interrupting

- **Follow-up** after a turn has finished, optionally with a different model:

  ```json
  {"task_id": "TASK", "prompt": "Now add a --json flag and update the tests.", "request_id": "follow-1"}
  ```

- **Steer** a running turn. Use the current `turn_id` from `codex_get`:

  ```json
  {"task_id": "TASK", "prompt": "Skip the docs for now; focus on the failing test.", "mode": "steer", "expected_turn_id": "TURN", "request_id": "steer-1"}
  ```

- **Interrupt** with `codex_interrupt` (`task_id`, `expected_turn_id`, `request_id`). The task becomes `interrupting`, then `interrupted` once Codex confirms.

Only one independent task may be active in a directory (or a directory nested inside it) at a time. For parallel work, use separate directories, for example separate Git worktrees.

## Native subagents

Ask the parent task to delegate with `codex_delegate`. Codex spawns its own subagents; the bridge does not spawn them itself:

```json
{"task_id": "TASK", "prompt": "Use one subagent to inspect the parser and another to inspect its tests. Report findings before editing.", "request_id": "delegate-1"}
```

For a parent that is currently running, also pass `expected_turn_id`.

- `codex_subagents` lists the verified children. Each child gets its own `task_id`.
- Polling the parent's events includes the children's events and requests (`include_descendants` defaults to true).
- `family_status` stays `active` while any child is still working. A finished parent turn does not mean every child is done.
- If a child has `can_accept_direct_input: false`, send instructions through the parent with `codex_delegate`.
- If cancelling a child's approval leaves the parent waiting, interrupt the parent's turn, wait for `interrupted`, then send a follow-up that explains what to do instead.

## Settings: model, plan mode, reviewer, access

`codex_models` lists the available models, with their reasoning efforts and service tiers. Use only combinations it lists. `codex_settings` reads or changes a task's settings:

```json
{"task_id": "TASK", "scope": "next", "collaboration_mode": "plan", "request_id": "plan-1"}
```

| Scope | Effect |
|---|---|
| `get` | Read the current settings (no `request_id` needed) |
| `next` | Applies to future turns: `model`, `effort`, `approval_mode`, `collaboration_mode`, `service_tier`, `access` |
| `current` | Applies to the running turn only (needs `expected_turn_id`): `model`, `effort`, `approval_mode`, `service_tier` |

Plan mode (`collaboration_mode: "plan"`) makes Codex propose a plan without implementing it. Switch back to `default` to implement. `access` is `read` or `write` within the same project. `service_tier: "default"` clears a previously chosen tier.

## Context files, images and skills

`codex_start`, `codex_send` and `codex_queue` accept extra input. Paths are relative to the project:

```json
{"task_id": "TASK", "prompt": "Use the spec and screenshot to propose a fix.", "context_files": ["docs/spec.md"], "images": ["screenshots/bug.png"], "request_id": "context-1"}
```

- `context_files`: up to 20 UTF-8 text files, 256 KiB each and 512 KiB in total.
- `images`: up to 10 PNG, JPEG, WebP or GIF files, 20 MiB each. They are copied to private storage before Codex reads them.
- `skills`: names from `codex_skills`.
- `output_schema` (start/send only): a JSON Schema for Codex's final answer.

Symbolic links are not followed.

## Review, fork, queue and other controls

| Goal | Tool and key arguments |
|---|---|
| Code review | `codex_review` with `target`: `uncommittedChanges`, `baseBranch` (+ `branch`), `commit` (+ `commit`) or `custom` (+ `instructions`) |
| Branch a conversation | `codex_fork` (optional `name`, `last_turn_id`). History only; no Git worktree is created |
| Queue follow-ups | `codex_queue` with `action`: `list`, `add`, `update`, `delete`, `reorder`, `start`. An idle task may run an added entry immediately |
| See the diff | `codex_diff` (the diff Codex reported for the latest turn) |
| Shrink history | `codex_compact` (uses model quota) |
| Rename / archive / restore | `codex_rename`, `codex_archive` (`action`: `archive` or `unarchive`) |
| Undo conversation turns | `codex_revert` with `before_turn_id` and `confirm_history_only: true`. **Files are not reverted** |
| Delete permanently | `codex_delete` with `confirm_thread_id` set to the task's `thread_id` |
| Background processes | `codex_terminals` (`list`, `terminate`, `clean`) |
| Persistent goal | `codex_goal`. Change it only when the user explicitly asks (`user_requested: true`) |
| Import a CLI session | `codex_sessions` (`list`, then `import` with `thread_id` and `confirm_stopped: true`) |
| Account limits | `codex_usage` |

Every action that changes something needs a unique `request_id`. See the [tool reference](tools.md) for all arguments.

## After a restart or disconnect

- **The MCP client disconnected or restarted.** Nothing is lost: the broker keeps running. Continue polling `codex_events` from your saved cursor.
- **The broker or Codex restarted.** Active tasks become `needs_resume`, and pending requests expire. Call `codex_resume`, look at `codex_history`, then send a follow-up if more work is needed. Nothing is replayed automatically.
- **A status is `uncertain`.** The acknowledgement for an action was lost. Check `codex_get` and `codex_history` before trying again. Do not repeat the work blindly.

## Tips for good results

- Give Codex a concrete goal, the expected result and how to test it.
- Never put passwords, tokens or keys in prompts or answers.
- Report results honestly: an `interrupted` or `failed` turn is not a success. Check the command exit codes in `codex_get`.
- Copy [examples/CLAUDE.md](../examples/CLAUDE.md) into your project so the main agent follows these rules.
