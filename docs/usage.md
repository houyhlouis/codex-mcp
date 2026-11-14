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
- [A complete walk-through](#a-complete-walk-through)
- [Tips for good results](#tips-for-good-results)

## The basic loop

1. **Choose a project.** The main agent picks an absolute directory. `codex_projects` can create it and returns a `project_id`:

   ```json
   {"action": "create", "path": "/Users/me/projects/demo"}
   ```

   Selecting or creating the same path again returns the same `project_id`, so `codex_projects` takes no `request_id`. An existing directory can also be passed directly as `workdir`. The home directory itself, filesystem roots and the server's private folders are refused.

2. **Start a task.** `codex_start` returns a task immediately, while Codex works in the background:

   ```json
   {"project_id": "PROJECT_ID", "prompt": "Fix the failing parser tests and run them.", "request_id": "fix-parser-1"}
   ```

   `request_id` is a unique key that you choose for this action. If the reply is lost, repeat the call with the same key and the same arguments: you get the saved result, and no second task is started. A key is tied to its arguments, so using it again with different arguments is an error. If the first attempt failed, or its outcome is unknown, repeating the call tells you so instead of trying again. [Which calls need a `request_id`](#which-calls-need-a-request_id) lists the tools that take a key.

3. **Follow progress.** Call `codex_events` with the last cursor (start at `0`). `wait_seconds` (up to 25) makes the call wait for new events:

   ```json
   {"task_id": "TASK", "after": 0, "wait_seconds": 25}
   ```

   Process the page, save the returned `cursor`, and call again. If `has_more` is true, fetch the next page right away. Every page also lists `pending_requests`.

   To save context while you only wait, add `"view": "compact"`. The events then come without their bodies, and you still see the status and every pending request. See [compact view](tools.md#compact-view).

4. **Answer requests.** See [Questions and approvals](#questions-and-approvals).

5. **Read the result.** When the status is `completed` (or `failed` / `interrupted`), `codex_get` returns the summary, changed files, commands with exit codes, plan and token usage.

   For a report, `codex_get` with `"view": "result"` is better: it returns one record for the turn that keeps what Codex *said* apart from the commands and file changes it *reported*, and tells you when something is missing. See [turn results](tools.md#turn-results-and-artifacts).

Each task is one persistent Codex conversation. Use the same `task_id` for related follow-up work.

**When a call fails,** the result has `"ok": false` and an `error` with a stable `code`. Two fields tell the main agent what is safe: `retryable` means the same call can simply be repeated, and `uncertain` means the action may already have happened, so the task must be inspected first. The [tool reference](tools.md#results-and-errors) lists all codes.

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

To answer a request, pass its `id` as `request_id` to `codex_respond` or `codex_review_request`. In these two tools `request_id` names the pending request. It is not the per-action key from the previous section.

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

**File approvals are tied to the patch you saw.** Each pending file approval carries a `proposal_hash`. The prompt and the terminal review send it back automatically. If Codex changes the patch while you are deciding, your answer is rejected and the request stays open, so that you can review the new patch. A main agent that is allowed to approve (`approval_relay`) must pass the same `proposal_hash` to `codex_respond`.

**File changes with hidden values need you.** If a patch contains something that looks like a credential, the server hides it in the copy shown for review and marks the request with a warning. Only a person can accept such a patch, in the client's prompt or the terminal. The main agent cannot, even with `approval_relay` enabled, because nobody can review what is hidden except you.

**Every decision leaves a receipt** that records who decided and for which patch. See [tools](tools.md#what-a-request-would-allow-and-who-decided).

**Large requests are shortened in lists.** A big patch appears as a summary with a preview. `codex_review_request` with `offset: 0` returns the full content in pages; see [Large requests](tools.md#large-requests).

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
- For work with a clear hand-over, add a `contract` to `codex_delegate`: the goal, what to deliver and how it will be checked. Codex can in turn attach options to a question, and the main agent answers by choosing one. See [structured questions and delegation contracts](tools.md#structured-questions-and-delegation-contracts).
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

See the [tool reference](tools.md) for all arguments.

### Which calls need a `request_id`

A call that changes something takes a `request_id`: a unique key that you choose, so that a repeated call is recognised instead of being run twice.

| Tool | Needs a `request_id` |
|---|---|
| `codex_start`, `codex_send`, `codex_resume`, `codex_interrupt`, `codex_delegate`, `codex_fork`, `codex_rename`, `codex_archive`, `codex_delete`, `codex_compact`, `codex_revert`, `codex_review` | Always |
| `codex_settings` | When `scope` is `next` or `current` |
| `codex_queue`, `codex_terminals`, `codex_sessions` | For every `action` except `list` |
| `codex_goal` | For `set` and `clear` |
| `codex_projects` and the tools that only read | Never |
| `codex_respond`, `codex_review_request` | Here `request_id` is the `id` of the pending request, not a key |

## After a restart or disconnect

- **The MCP client disconnected or restarted.** Nothing is lost: the broker keeps running. Continue polling `codex_events` from your saved cursor.
- **The broker or Codex restarted.** Active tasks become `needs_resume`, and pending requests expire. Call `codex_resume`, look at `codex_history`, then send a follow-up if more work is needed. Nothing is replayed automatically.
- **A status is `uncertain`.** The acknowledgement for an action was lost. Check `codex_get` and `codex_history` before trying again. Do not repeat the work blindly.
- **What exactly happened?** The result of `codex_resume` contains a [reconciliation report](tools.md#the-reconciliation-report): what was requested, what Codex acknowledged, what was restored from its history, and what is still unknown, with the safe next steps.
- **A project is still busy after its turn finished.** When a turn ends, the server asks Codex whether anything can still run in the project, and frees the project as soon as nothing can. If it stays busy, something really is pending: a queued instruction, an active goal, background terminals, or an action whose outcome is unknown. The refusal message and `workspace_claims` in `codex_get` name it. On Linux the server also looks for commands that Codex has lost track of, and names their process IDs. If work was running when the broker was stopped, the project stays reserved until you call `codex_resume` for that task, because commands from before the restart may still be running. Pause or clear the goal, empty the queue or stop the terminals; `codex_resume` checks again at once. See [Why a project stays busy](tools.md#why-a-project-stays-busy).
- **Some progress text is missing.** Under a flood of output the server skips streamed text and marks the place with a `bridge/event_gap` event. Final results and approvals are never skipped. `codex_history` has the complete output.
- **Results may be incomplete after a restart.** `codex_get` then shows `evidence_complete: false`, with the reason in `evidence_gaps`. Page through `codex_history` to see what Codex saved.

## A complete walk-through

This example follows one task from start to hand-over, including a question, an approval and a restart in the middle. The JSON blocks are the arguments of the tool calls that the main agent makes.

**1. Start.** The user asks for a change in an existing project and wants to decide approvals personally:

```json
{"workdir": "/Users/me/projects/shop", "prompt": "Add input validation to the checkout form and run the tests.", "approval_mode": "ask", "request_id": "checkout-start-1"}
```

`codex_start` returns at once with a `task_id` and the status `running`.

**2. Wait.** The main agent polls with the compact view, which costs little context:

```json
{"task_id": "TASK", "after": 0, "wait_seconds": 25, "view": "compact"}
```

It saves the returned `cursor` and passes it as `after` in the next call.

With several tasks running at once, one call can watch them all; see [watching several tasks](tools.md#watching-several-tasks).

**3. Codex asks the main agent a question.** A page lists a pending request with `"kind": "parent_question"`. The main agent knows the answer and replies itself:

```json
{"request_id": "PENDING_ID", "response": {"text": "Validate on the server as well. The tests are run with npm test."}}
```

**4. Codex asks for an approval.** Later a request with `"kind": "command_approval"` appears, because `npm install` needs the network. The main agent must not decide this. It shows the request to the user:

```json
{"request_id": "PENDING_ID"}
```

With `codex_review_request`, a client that supports form prompts shows the command, and the user accepts or declines. If the result says `"status": "pending"`, the client could not show a prompt, and the user runs this in a terminal instead:

```bash
python3 server.py review PENDING_ID
```

If the user says no in the chat, the main agent may send the denial itself:

```json
{"request_id": "PENDING_ID", "response": {"decision": "decline"}}
```

**5. The computer restarts.** The next poll after the restart shows the status `needs_resume`. The approval from step 4 has expired, and nothing is repeated automatically. The main agent reloads the conversation and looks at what Codex saved:

```json
{"task_id": "TASK", "request_id": "checkout-resume-1"}
```

```json
{"task_id": "TASK", "kind": "turns", "limit": 5}
```

The first call is `codex_resume`, the second `codex_history`. The history shows that the turn was interrupted before the tests ran, so the main agent sends a follow-up in the same conversation:

```json
{"task_id": "TASK", "prompt": "The machine restarted. Check the state of your changes, then finish and run the tests.", "request_id": "checkout-continue-1"}
```

**6. Hand over.** When the status is `completed`, the main agent reads the result of the turn:

```json
{"task_id": "TASK", "view": "result"}
```

It reports to the user what Codex *said* it did, which commands and file changes were *reported*, and anything listed under `evidence_completeness.gaps`. If the turn had ended as `failed` or `interrupted`, it would say that instead of calling the work done.

What to remember from this example:

- A new, unique `request_id` for every action (`checkout-start-1`, `checkout-resume-1`, ...). In steps 3 and 4 the `request_id` is the ID of the pending request instead.
- The main agent answers questions addressed to it, and only the user approves.
- After a restart: `codex_resume`, then `codex_history`, then decide. Never send the original prompt again blindly.

## Tips for good results

- Give Codex a concrete goal, the expected result and how to test it.
- Never put passwords, tokens or keys in prompts or answers.
- Report results honestly: an `interrupted` or `failed` turn is not a success. Check the command exit codes in `codex_get`.
- Copy [examples/CLAUDE.md](../examples/CLAUDE.md) into your project so the main agent follows these rules.
