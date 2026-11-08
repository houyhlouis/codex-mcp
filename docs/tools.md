# MCP tool reference

English | [简体中文](tools.zh-CN.md)

All tool arguments are JSON objects. Unknown fields are rejected. Responses have JSON in a text content block and, on MCP 2025-06-18 or later, matching `structuredContent`. Operational errors use `isError: true`; malformed tool arguments produce JSON-RPC `-32602`.

The server exposes no arbitrary shell, filesystem-read, configuration-write, login/logout, token, or raw app-server RPC tool.

## Tool list

| Tool | Required arguments | Optional arguments and defaults | Result/use |
|---|---|---|---|
| `codex_status` | None | None | CLI version, backend, sign-in type, pending requests, journal high water; starts Codex if needed |
| `codex_projects` | None for list; absolute `path` for select/create | `action: list`, `path`, `offset: 0`, `limit: 100` | Previously selected projects, or a newly selected/created project descriptor |
| `codex_models` | None | `cursor`, `limit: 100` | Live catalog page: model IDs, supported/default reasoning efforts, `nextCursor` |
| `codex_start` | `prompt`, `request_id`; exactly one of `workdir` or `project_id` | `model`, `effort`, `access: write`, `approval_mode: inherit`, `collaboration_mode: default`, `service_tier`, `name`, rich inputs below | New persistent task and first turn acknowledgement |
| `codex_list` | None | `offset: 0`, `limit: 50` | Newest-first task snapshots and `next_offset` |
| `codex_get` | `task_id` | None | Current state, latest summary, files, commands, plan, usage, requests |
| `codex_events` | None | `task_id`, `after: 0`, `limit: 100`, `wait_seconds: 0`, `include_descendants: true` | Durable events, next `cursor`, `has_more`, current pending requests |
| `codex_send` | `task_id`, `prompt`, `request_id` | `mode: follow_up`, `expected_turn_id`, `model`, `effort`, rich inputs below | Follow-up turn or active steering acknowledgement |
| `codex_resume` | `task_id`, `request_id` | None | Load saved conversation and reconcile recent turn state; no prompt replay |
| `codex_interrupt` | `task_id`, `expected_turn_id`, `request_id` | None | Interrupt acknowledgement; poll until the turn actually ends |
| `codex_history` | `task_id` | `kind: turns`, `cursor`, `limit: 50`, `turn_id` | Codex history page with `nextCursor`; `kind` can be `turns` or `items` |
| `codex_requests` | None | `task_id` | Pending questions/approvals; ignores resolved and expired requests |
| `codex_respond` | `request_id`, `response` | None | Send a validated response to a live pending request |
| `codex_review_request` | `request_id` | None | Use client human elicitation, or return a pending/local-review fallback |
| `codex_capabilities` | `task_id` | None | Native collaboration modes, permission-profile catalog and direct-input capability |
| `codex_settings` | `task_id`; `request_id` for changes | `scope: get` (`get/next/current`), `expected_turn_id`, `model`, `effort`, `approval_mode`, `collaboration_mode`, `service_tier`, `access` | Inspect/update scoped settings; current-turn settings do not change future turns |
| `codex_subagents` | `task_id` | `refresh: true`, `cursor`, `limit: 50`, `archived: false` | Discover, verify and subscribe to native descendants; return child task IDs |
| `codex_delegate` | `task_id`, `prompt`, `request_id` | `expected_turn_id` for an active parent | Ask the parent to spawn or control native subagents |
| `codex_fork` | `task_id`, `request_id` | `name`, `last_turn_id` | Fork native history without starting work or continuing a goal |
| `codex_rename` | `task_id`, `name`, `request_id` | None | Rename the native conversation |
| `codex_archive` | `task_id`, `request_id` | `action: archive` (`archive/unarchive`) | Archive/restore an idle family |
| `codex_delete` | `task_id`, `confirm_thread_id`, `request_id` | None | Delete native history and descendants; require exact native ID; retain bridge audit |
| `codex_compact` | `task_id`, `request_id` | None | Start native compaction; poll events; may consume usage |
| `codex_revert` | `task_id`, `before_turn_id`, `confirm_history_only: true`, `request_id` | None | Remove that turn and later native history; does not undo files |
| `codex_queue` | `task_id`; `request_id` for mutations | `action: list` (`list/add/update/delete/reorder/start`), `prompt`, `queued_id`, `ordered_ids`, `cursor`, `limit: 50`, images/context files/skills | Manage native queued submissions; add can trigger immediate execution |
| `codex_review` | `task_id`, `request_id` | `target: uncommittedChanges` (`uncommittedChanges/baseBranch/commit/custom`), `branch`, `commit`, `instructions` | Start an inline native code-review turn |
| `codex_terminals` | `task_id`; `request_id` for mutations | `action: list` (`list/terminate/clean`), `process_id`, `cursor`, `limit: 50` | List/stop owned background processes, not arbitrary shell execution |
| `codex_goal` | `task_id`; `request_id` and `user_requested: true` for mutations | `action: get` (`get/set/clear`), `objective`, `status`, `token_budget` | Native persistent goal; no implicit budget or inferred user intent |
| `codex_skills` | `task_id` | `force_reload: false` | Available native skill metadata; select enabled unique names in input |
| `codex_diff` | `task_id` | None | Latest Codex turn diff and file list, not a separate Git audit |
| `codex_usage` | None | None | Native account rate-limit/allowance information; no billing changes |
| `codex_sessions` | `workdir` or `project_id`; `thread_id`, `confirm_stopped: true`, `request_id` for import | `action: list` (`list/import`), `cursor`, `limit: 50`, `archived: false` | Discover/import native root conversations at the exact approved project cwd |

All 32 tools use the same closed dispatch allowlist. Mutation actions on multi-action tools require an operation key even when the schema also permits a read-only action without one.

`approval_mode` accepts `inherit`, `ask`, or `approve_for_me` on start; settings changes select `ask` or `approve_for_me`. These map to native `user`/`auto_review` reviewers. The automatic reviewer can approve or deny; its published review events and latest result are retained. It does not imply unrestricted access. `collaboration_mode` is `default` or `plan`. Service tiers must be offered by the chosen model; `default` resets a settings tier. Access remains limited to `read` or `write` within the same project. Current-turn changes require `expected_turn_id` and cannot change access or collaboration mode.

Rich inputs on start/send: `images` (up to 10 relative project paths), `context_files` (up to 20 UTF-8 files), `skills` (up to 10 enabled unique catalog names), and `output_schema` (native structured final-output schema, new turns only). Text files are limited to 256 KiB each and 512 KiB combined. Images are limited to 20 MiB each. Files are opened without following symlink components; images are copied into private broker state before native ingestion so a project process cannot race the path. These image snapshots are retained with state. Queue add/update supports rich inputs except final-output schemas.

Limits are 1–200 records per page, 0–25 seconds per poll, and 100,000 characters per prompt. Event pages also have a roughly 1 MiB budget; a single larger event is returned intact. The Codex frame ceiling is 16 MiB and the broker response ceiling is 32 MiB. Exceeding these limits stops the affected connection rather than silently discarding part of a frame.

Use exact model/effort pairs from `codex_models`. A model listed in a catalog can still be unavailable to the account for a particular inference request; report that failure to the user. Changing models is supported on a new follow-up turn, not during a steer.

Projects are selected or created by the main agent, with no workspace configuration. Pass an absolute workdir or a returned project_id. Projects are explicit per task. `codex_projects(action="select")` does not establish a process-global current project. Creation makes ordinary directories; it does not clone repositories, initialize Git, create worktrees, or install dependencies.

## Task and turn semantics

A task ID is a bridge-owned persistent conversation. `thread_id` identifies the corresponding native Codex conversation. Each initial/follow-up instruction starts a new turn in that conversation.

`family_status` is `active` while this task or a known descendant is active/queued/recovering, otherwise `idle`. A completed parent turn does not necessarily mean all child work finished; inspect the child statuses and pending requests.

| State | Meaning |
|---|---|
| `starting` | A control operation is being acknowledged |
| `queued` | Pending native work reserves this task's project family |
| `running` | A turn is active |
| `waiting` | A Codex request awaits a response |
| `interrupting` | Interrupt was acknowledged; completion is still pending |
| `completed` | The latest turn completed successfully |
| `failed` | Task creation or the latest turn failed; inspect error/events |
| `interrupted` | Codex confirmed interruption |
| `needs_resume` | Broker/backend lost its live connection; saved history remains available |
| `uncertain` | A control acknowledgement was lost; do not repeat work blindly |
| `idle` | A saved conversation has been loaded and has no confirmed active turn |
| `archived` | Restore before continuing this conversation |
| `deleted` | Native history was deleted; bridge audit records remain |

An idle resumed task can include `recovered_turn_status` describing its latest persisted turn. Inspect `codex_history` for authoritative recovered items. Resumption does not restart an interrupted command or reproduce all lost stream deltas.

Only one independent active task family may use a given project or overlapping directory. Verified native descendants may collaborate within that family. The configured concurrent-task limit counts active root families; native Codex separately controls its internal agent concurrency. Separate worktrees in sibling directories can be selected as independent projects. The bridge does not manage Git worktree lifecycle.

`summary` is the latest agent-authored message, which can be interim while work is active. `files` are paths reported by file-change items. `commands` keeps the latest 100 command records with exit codes; complete records and output remain in events/history. A successful shell command does not, by itself, prove every requested test passed. Read its output and the final agent report.

## Replay and retry

`after` is an exclusive, monotonically increasing journal sequence. It is not a timestamp or Codex thread cursor. Process a page before advancing to its returned `cursor`. If `has_more` is true, fetch the next page immediately. Do not reuse a filtered task cursor as an unfiltered stream cursor.

The event stream includes agent text deltas, completed items, command output, changes, plans, usage, automatic-review results and bridge lifecycle events. Parent polling includes verified descendants by default; each record retains its originating `task_id`. Raw private reasoning content is excluded; available reasoning summaries can be included. `bridge/request` carries a stable pending-request ID plus parent/root task IDs. The same requests also appear in polling results, so notification loss cannot hide them.

`request_id` in start/send/resume/interrupt and the new control mutations is an **operation idempotency key**. A repeated identical call returns its saved acknowledgement; that snapshot can be older than `codex_get`. Reusing a key with different arguments fails. If delivery was uncertain, the key remains uncertain and will not cause a replay.

`request_id` in respond/review is instead a **pending Codex request ID** returned by this server. Resolved, expired, interrupted and old-process requests cannot be answered. Responses are not automatically resent after transport failure.

## Response shapes

For `parent_question`, the main agent can provide task context without a human approval:

```json
{"request_id":"PENDING_ID","response":{"text":"Use the existing parser interface and run its unit tests."}}
```

For `command_approval` and `file_approval`, send one offered decision:

```json
{"request_id":"PENDING_ID","response":{"decision":"accept"}}
```

Supported decisions are `accept`, `decline`, and `cancel`, subject to Codex's `availableDecisions`. Some commands offer `accept` and `cancel` but **not** `decline`. In that case use `cancel` to deny; it may interrupt the turn. `acceptForSession`, policy amendments and persistent network-rule changes are rejected. File approvals include the proposed patch when supplied by Codex and must stay inside the task project.

For built-in `user_input`, answer all question IDs:

```json
{
  "request_id":"PENDING_ID",
  "response":{"answers":{"choice":{"answers":["Use the current schema."]}}}
}
```

For `permissions`, grant only a subset of what was requested, for this turn:

```json
{
  "request_id":"PENDING_ID",
  "response":{"permissions":{"network":{"enabled":true}},"scope":"turn"}
}
```

An empty `permissions` object grants nothing. Filesystem grants must be concrete paths within the task's project. Special-path/glob grants and session grants are rejected. The bridge validates that grants do not introduce permissions absent from the request.

For external MCP `elicitation` requests, supported simple forms use:

```json
{"request_id":"PENDING_ID","response":{"action":"accept","content":{"choice":"yes"}}}
```

`decline`/`cancel` sends `content: null`. URL-mode requests can relay completion using `action: "accept", content: null` only after the user completes the provider's flow. The bridge does not visit the URL or fetch tokens. Advanced schemas fail closed; secret fields must use native/provider authentication rather than MCP arguments.

Except for parent questions and denials, these responses require either `codex_review_request`'s human UI, interactive terminal review, or the operator's explicit `approval_relay = true` configuration. The `response` object is method-specific data, never an arbitrary app-server method.

## CLI tool calls

The diagnostic CLI uses the identical allowlist and validation. Run from the source root:

```bash
python3 server.py --config /absolute/config.toml \
  call codex_events --args '{"task_id":"TASK_ID","after":0,"wait_seconds":25}'
```

`--args -` reads JSON from stdin, avoiding arguments in the process list. Do not transmit credentials through task prompts or answers.
