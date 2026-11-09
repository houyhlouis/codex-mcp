# Tool reference

[Back to README](../README.md) · [Usage guide](usage.md)

The server exposes 32 tools. All arguments are JSON objects, and unknown fields are rejected. Results are returned as JSON text. On MCP 2025-06-18 and newer they are also returned as `structuredContent`. Operational errors return `isError: true`; malformed arguments return JSON-RPC error `-32602`.

There is deliberately no tool for arbitrary shell commands, file reads, configuration changes, login/logout, tokens or raw Codex protocol calls.

- [Two kinds of `request_id`](#two-kinds-of-request_id)
- [Projects, status and models](#projects-status-and-models)
- [Tasks](#tasks)
- [Progress and history](#progress-and-history)
- [Questions and approvals](#questions-and-approvals)
- [Settings and subagents](#settings-and-subagents)
- [Conversation controls](#conversation-controls)
- [Rich input](#rich-input)
- [Task states](#task-states)
- [Response formats for `codex_respond`](#response-formats-for-codex_respond)
- [Limits](#limits)
- [Calling tools from the command line](#calling-tools-from-the-command-line)

## Two kinds of `request_id`

| Where | Meaning |
|---|---|
| Tools that change something (`codex_start`, `codex_send`, `codex_fork`, …) | An **operation key** that you choose. Repeating a call with the same key and identical arguments returns the saved result instead of doing the work twice. The same key with different arguments is rejected. If the first attempt failed or its outcome is uncertain, the key stays blocked; inspect the task, then use a new key. |
| `codex_respond`, `codex_review_request` | The **pending request ID** that the server returned for a question or approval. |

Multi-action tools (`codex_settings`, `codex_queue`, `codex_terminals`, `codex_goal`, `codex_sessions`) need an operation key only for actions that change something.

## Projects, status and models

| Tool | Required | Optional (default) | Returns |
|---|---|---|---|
| `codex_status` | none | none | Codex version, sign-in type (no credentials), pending requests, event cursor. Starts Codex if needed |
| `codex_projects` | `path` for `select`/`create` | `action` (`list`), `offset` (0), `limit` (100) | Previously selected projects, or the selected/created project with its `project_id` |
| `codex_models` | none | `cursor`, `limit` (100) | Model catalog page: model IDs, supported and default reasoning efforts, service tiers, `nextCursor` |
| `codex_usage` | none | none | Account rate limits and remaining allowance. Read-only |
| `codex_capabilities` | `task_id` | none | Collaboration modes, permission profiles and direct-input capability for a task |
| `codex_skills` | `task_id` | `force_reload` (false) | Skills available in the task's project |

`codex_projects` with `create` makes an ordinary directory. It does not clone repositories, run `git init`, create worktrees or install dependencies. A model listed in the catalog may still be unavailable to your account; such a failure is reported by the turn.

## Tasks

| Tool | Required | Optional (default) | What it does |
|---|---|---|---|
| `codex_start` | `prompt`, `request_id`, and exactly one of `workdir` or `project_id` | `model`, `effort`, `access` (`write`), `approval_mode` (`inherit`), `collaboration_mode` (`default`), `service_tier`, `name`, [rich input](#rich-input) | Creates a persistent conversation and starts the first turn. Returns immediately |
| `codex_send` | `task_id`, `prompt`, `request_id` | `mode` (`follow_up`), `expected_turn_id`, `model`, `effort`, [rich input](#rich-input) | Starts a follow-up turn, or with `mode: "steer"` adds input to the running turn |
| `codex_interrupt` | `task_id`, `expected_turn_id`, `request_id` | none | Asks Codex to stop the turn. Poll until the status is `interrupted` |
| `codex_resume` | `task_id`, `request_id` | none | Reloads a saved conversation after a restart. Never replays a prompt |
| `codex_list` | none | `offset` (0), `limit` (50) | Tasks, newest first |
| `codex_get` | `task_id` | none | Status, latest summary, changed files, last 100 commands with exit codes, plan, usage, pending requests, subagents |

`summary` is the latest message written by Codex and can be an interim message while work is running. A command's exit code 0 does not by itself prove that all requested tests passed; read the output and the final report.

## Progress and history

| Tool | Required | Optional (default) | Returns |
|---|---|---|---|
| `codex_events` | none | `task_id`, `after` (0), `limit` (100), `wait_seconds` (0, max 25), `include_descendants` (true) | Journal events after the cursor, the next `cursor`, `has_more`, and the current pending requests |
| `codex_history` | `task_id` | `kind` (`turns` or `items`), `cursor`, `limit` (50), `turn_id` | Codex's own persisted history, for recovery after an outage |
| `codex_diff` | `task_id` | none | The latest turn diff and file list reported by Codex (not an independent Git diff) |

How to use event cursors:

- `after` is exclusive. Process a page before saving its `cursor`. Fetch again immediately while `has_more` is true.
- A cursor from a task-filtered call is not valid for unfiltered calls, and vice versa.
- Events include Codex's messages, completed items, command output, file changes, plans, usage, automatic-review results and `bridge/*` lifecycle events. Raw private reasoning is never included.
- Pending requests are part of every page, so missed notifications cannot hide them.

## Questions and approvals

| Tool | Required | What it does |
|---|---|---|
| `codex_requests` | none (optional `task_id`) | Lists pending questions and approvals, including those from subagents |
| `codex_respond` | `request_id`, `response` | Sends a validated answer. See [response formats](#response-formats-for-codex_respond) |
| `codex_review_request` | `request_id` | Shows the request to the human through the client's form prompt. Without prompt support, it returns `status: "pending"` and the request must be reviewed in a terminal |

Positive approvals need the human prompt, the terminal (`server.py review REQUEST_ID`) or the operator setting `approval_relay = true`. Denials and answers to `parent_question` can always be sent by the main agent.

## Settings and subagents

| Tool | Required | Optional (default) | What it does |
|---|---|---|---|
| `codex_settings` | `task_id`; `request_id` for changes | `scope` (`get`, `next`, `current`), `expected_turn_id`, `model`, `effort`, `approval_mode` (`ask`/`approve_for_me`), `collaboration_mode` (`default`/`plan`), `service_tier`, `access` (`read`/`write`) | Reads or changes settings. `current` applies to the running turn only and cannot change access or collaboration mode |
| `codex_delegate` | `task_id`, `prompt`, `request_id` | `expected_turn_id` (required if the parent is running) | Asks the parent to spawn or control its native subagents |
| `codex_subagents` | `task_id` | `cursor`, `limit` (50), `refresh` (true), `archived` (false) | Discovers and verifies native descendants and returns their task IDs |

`approval_mode` values: `inherit` (start only; keep Codex's configured reviewer), `ask` (human reviewer), `approve_for_me` (Codex's automatic reviewer `auto_review`). Service tiers must be offered by the chosen model; `default` clears a tier.

## Conversation controls

All of these need `task_id` and `request_id`. Most require the task family to be idle.

| Tool | Extra arguments | What it does |
|---|---|---|
| `codex_review` | `target` (`uncommittedChanges`, `baseBranch` + `branch`, `commit` + `commit`, `custom` + `instructions`) | Starts a native code-review turn |
| `codex_fork` | `name`, `last_turn_id` (optional) | Copies the conversation history into a new task. Does not start work |
| `codex_rename` | `name` | Renames the conversation |
| `codex_archive` | `action` (`archive` or `unarchive`) | Archives or restores the conversation and its subagents |
| `codex_delete` | `confirm_thread_id` (must equal the task's `thread_id`) | Permanently deletes the native conversation and subagents. The bridge's audit records remain |
| `codex_compact` | none | Compacts the conversation history. May use model quota |
| `codex_revert` | `before_turn_id`, `confirm_history_only: true` | Removes that turn and all later turns from the conversation. **Does not revert files** |
| `codex_queue` | `action` (`list`, `add`, `update`, `delete`, `reorder`, `start`), `prompt`, `queued_id`, `ordered_ids`, `cursor`, `limit`, rich input | Manages queued instructions. `list` needs no `request_id`. An idle task may consume an added entry immediately |
| `codex_terminals` | `action` (`list`, `terminate`, `clean`), `process_id`, `cursor`, `limit` | Lists or stops the task's background processes. `list` needs no `request_id` |
| `codex_goal` | `action` (`get`, `set`, `clear`), `objective`, `status`, `token_budget`, `user_requested` | Native persistent goal. Changes require `user_requested: true` and must come from an explicit user request |
| `codex_sessions` | `workdir` or `project_id`; `action` (`list`, `import`), `thread_id`, `confirm_stopped`, `cursor`, `limit`, `archived` | Lists or imports existing Codex CLI conversations from exactly this project. Import requires `confirm_stopped: true` |

## Rich input

Accepted by `codex_start`, `codex_send` and `codex_queue` (`add`/`update`):

| Field | Limits |
|---|---|
| `context_files` | Up to 20 UTF-8 files relative to the project; 256 KiB each, 512 KiB in total |
| `images` | Up to 10 PNG/JPEG/WebP/GIF files relative to the project; 20 MiB each. Copied to private storage before Codex reads them |
| `skills` | Up to 10 enabled skill names from `codex_skills` |
| `output_schema` | JSON Schema for the final answer (start/send only) |

Paths must stay inside the project. Symbolic links are refused.

## Task states

| Status | Meaning |
|---|---|
| `starting` | An action is waiting for Codex's acknowledgement |
| `queued` | A queued instruction is waiting to run |
| `running` | A turn is active |
| `waiting` | A question or approval needs an answer |
| `interrupting` | Interrupt requested; waiting for confirmation |
| `completed` | The latest turn finished successfully |
| `failed` | Task creation or the latest turn failed; see `error` and events |
| `interrupted` | Codex confirmed the interruption |
| `needs_resume` | The connection to Codex was lost; call `codex_resume` |
| `uncertain` | An acknowledgement was lost; inspect before retrying |
| `idle` | Loaded, with no active turn |
| `archived` | Restore with `codex_archive` before continuing |
| `deleted` | Native history was deleted; audit records remain |

`family_status` is `active` while the task or any known subagent is active, queued or awaiting recovery.

## Response formats for `codex_respond`

**`parent_question`**

```json
{"request_id": "PENDING_ID", "response": {"text": "Use the existing parser interface."}}
```

**`command_approval` and `file_approval`.** One of `accept`, `decline` or `cancel`, and only if Codex offered it in `availableDecisions`. If `decline` is not offered, use `cancel` to deny (this may interrupt the turn). Session-wide approvals and rule changes are not supported. File approvals are refused if the patch would touch anything outside the project.

```json
{"request_id": "PENDING_ID", "response": {"decision": "accept"}}
```

**`user_input`.** Answer every question ID. Questions marked secret are refused; use Codex's own sign-in flow for credentials.

```json
{"request_id": "PENDING_ID", "response": {"answers": {"choice": {"answers": ["Use the current schema."]}}}}
```

**`permissions`.** Grant a subset of what was requested, for this turn only. `{}` grants nothing. Filesystem paths must be inside the project (relative paths are relative to the project); glob and special paths are refused.

```json
{"request_id": "PENDING_ID", "response": {"permissions": {"network": {"enabled": true}}, "scope": "turn"}}
```

**`elicitation`** (forms from external MCP tools):

```json
{"request_id": "PENDING_ID", "response": {"action": "accept", "content": {"choice": "yes"}}}
```

`decline` and `cancel` send no content. For URL-mode requests, send `accept` with `content: null` only after the user has completed the provider's page. Forms with advanced schemas or secret fields are refused and must be handled in Codex directly.

## Limits

| Limit | Value |
|---|---|
| Prompt length | 100,000 characters |
| Page size | 1–200 records |
| Poll wait | 0–25 seconds |
| Event page size | about 1 MiB (a single larger event is still returned whole) |
| MCP input frame | 1 MiB |
| Codex frame / broker response | 16 MiB / 32 MiB |
| Concurrent independent task families | `max_tasks` (default 4) |

## Calling tools from the command line

The CLI uses the same validation and allowlist. This is useful for diagnosis:

```bash
python3 server.py call codex_events --args '{"task_id": "TASK", "after": 0, "wait_seconds": 25}'
```

`--args -` reads the JSON from stdin, which keeps it out of the process list.
