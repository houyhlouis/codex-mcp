# Tool reference

[Back to README](../README.md) · [Usage guide](usage.md)

The server exposes 34 tools. All arguments are JSON objects, and unknown fields are rejected. Results are returned as JSON text. On MCP 2025-06-18 and newer they are also returned as `structuredContent`, and each tool publishes an output schema. Failures have a stable `code`; see [Results and errors](#results-and-errors).

There is deliberately no tool for arbitrary shell commands, file reads, configuration changes, login/logout, tokens or raw Codex protocol calls.

- [Two kinds of `request_id`](#two-kinds-of-request_id)
- [Results and errors](#results-and-errors)
- [Projects, status and models](#projects-status-and-models)
- [Tasks](#tasks)
- [Progress and history](#progress-and-history)
- [Questions and approvals](#questions-and-approvals)
- [Settings and subagents](#settings-and-subagents)
- [Conversation controls](#conversation-controls)
- [Rich input](#rich-input)
- [Task states](#task-states)
- [Response formats for `codex_respond`](#response-formats-for-codex_respond)
- [MCP 2026-07-28 and Tasks](#mcp-2026-07-28-and-tasks)
- [Limits](#limits)
- [Calling tools from the command line](#calling-tools-from-the-command-line)

## Two kinds of `request_id`

| Where | Meaning |
|---|---|
| Tools that change something (`codex_start`, `codex_send`, `codex_fork`, …) | An **operation key** that you choose. Repeating a call with the same key and identical arguments returns the saved result instead of doing the work twice. The same key with different arguments is rejected. If the first attempt failed or its outcome is uncertain, the key stays blocked; inspect the task, then use a new key. A call that the server refuses before it has sent anything to Codex (the project is busy, the turn has changed, the task is in the wrong state) is saved as a finished refusal: the same key returns the same error, and nothing is left unknown. |
| `codex_respond`, `codex_review_request` | The **pending request ID** that the server returned for a question or approval. |

Because one name for two things is easy to mix up, each kind also has a name of its own:

| Clear name | Same as | Used by |
|---|---|---|
| `operation_id` | `request_id` as an operation key | every tool that changes something |
| `pending_request_id` | `request_id` as a pending request ID | `codex_respond`, `codex_review_request` |

Use either name. `request_id` keeps working. If you send both, they must be equal. Results repeat the value under the clear name and add `request_id_kind`, which is `"operation"` or `"pending_request"`.

Multi-action tools (`codex_projects`, `codex_settings`, `codex_queue`, `codex_terminals`, `codex_goal`, `codex_sessions`) need an operation key only for actions that change something. Each action accepts only its own arguments. An argument that belongs to a different action is rejected with a message such as `codex_queue action=delete: argument prompt does not apply`, so that a mistake is not silently ignored. The one exception is an argument that only repeats its default value, which some clients add to every call: it is ignored.

## Results and errors

Every successful result has two extra fields, `"ok": true` and `"result_version": 1`. All other fields are the same as in earlier versions.

`ok` describes the tool call, not Codex's work. Reading a task whose turn failed is a successful call: `ok` is `true` and the task's `status` is `failed`.

A failed call returns `isError: true` with this content:

```json
{
  "result_version": 1,
  "ok": false,
  "error": {
    "code": "stale_turn",
    "message": "...",
    "retryable": false,
    "uncertain": false,
    "next_steps": ["Refresh codex_get before controlling the current turn"],
    "ids": {"task_id": "TASK", "expected_turn_id": "TURN"}
  }
}
```

| Field | Meaning |
|---|---|
| `code` | A stable name for the kind of failure. Decide what to do from the code, not from the message |
| `message` | A description for people. Its wording can change between versions |
| `retryable` | `true` only when repeating the same call is safe and may succeed |
| `uncertain` | `true` when the server cannot tell whether the action happened. Inspect the task before doing anything else. Do not simply repeat a call that changes something |
| `next_steps` | Short suggestions for what to do next |
| `ids` | The IDs from your call that the error is about |

Some errors add fields, for example `native_code` when Codex itself refused the request. Malformed arguments are returned as JSON-RPC error `-32602`; its `data` holds the same content.

### Error codes

**Problems with the call**

| Code | Meaning | What to do |
|---|---|---|
| `invalid_arguments` | An argument is missing, unknown, out of range, or does not belong to the chosen action | Correct the call |
| `invalid_response` | The answer sent with `codex_respond` does not fit the request | See [response formats](#response-formats-for-codex_respond) |
| `not_found` | Unknown task, pending request or project | Refresh with `codex_list`, `codex_requests` or `codex_projects` |
| `operation_conflict` | The operation key was already used with different arguments | Use a new key for new work |
| `user_request_required` | A goal was changed without `user_requested: true` | Change goals only when the user asked for it |
| `mcp_version_unsupported` | A request named an MCP version the server does not support (MCP 2026-07-28 only) | Choose a version listed by `server/discover` |
| `mcp_capability_required` | A request needs a capability the client did not declare, such as Tasks or forms (MCP 2026-07-28 only) | Declare the capability on that request, or use the ordinary tools |
| `unsupported_task_operation` | A Tasks method other than get, update and cancel | Use one of the three |
| `result_too_large` | The requested page does not fit in one result | Ask for a smaller page |
| `artifact_not_found` | Unknown artifact, or it belongs to another task | Read the references again with `view: "result"` |
| `artifact_too_large` | The content is larger than 32 MiB and was not captured | Nothing to retry; the record says so |

**The task is not ready for this action**

| Code | Meaning | What to do |
|---|---|---|
| `state_conflict` | The task's current state does not allow the action | Read the task with `codex_get` |
| `stale_turn` | `expected_turn_id` is no longer the running turn | Refresh with `codex_get` |
| `stale_request` | The pending request was already answered, has expired, or is being answered | Refresh with `codex_requests` |
| `stale_proposal` | `proposal_hash` is missing or does not match the current file changes | Review the current request and send its hash |
| `decision_mismatch` | A structured answer does not match the question, the offered options or the referenced evidence | Read the request again and use its `id`, `context_hash` and options |
| `decision_expired` | The deadline of a structured question or delegation contract has passed | Answer in plain text, or start a new delegation with a new key |
| `human_review_required` | Only a person may approve this request | Use `codex_review_request` or `server.py review` |
| `person_review_required` | A file change with hidden values can be accepted by a person only, also when `approval_relay` is enabled | Use `codex_review_request` or `server.py review`, and read the warning |
| `workspace_busy` | Another task still reserves the project, or `max_tasks` is reached | See [Why a project stays busy](#why-a-project-stays-busy) |
| `workspace_read_unavailable` | The server could not check what is still running in the project, so the reservation was kept | Call `codex_resume`; use a tested Codex version |
| `scope_unverified` | The server could not confirm that the conversation belongs to this project | Call `codex_resume` and check `codex_history` |
| `artifact_version_mismatch` | The file or stored content no longer matches the reference | Read the current result; do not use old content |

**Old data was cleaned up**

| Code | Meaning | What to do |
|---|---|---|
| `cursor_expired` | The cursor points into events or requests that were removed | Continue from `resume_after`. For pending requests, start again without a cursor |
| `operation_expired` | The saved result of this operation key was removed. The key stays blocked and is never run again | Inspect the task; use a new key for new work |
| `result_expired` | The record of this turn was removed | Use `codex_history`. Do not treat the missing record as complete |

**The outcome is unknown** (`uncertain` is `true`)

| Code | Meaning | What to do |
|---|---|---|
| `operation_uncertain` | An earlier attempt with this key may or may not have taken effect | Inspect with `codex_get` and `codex_events`, then use a new key if work is still needed |
| `native_timeout` | Codex did not acknowledge the request in time | Inspect the task. A read can simply be repeated |
| `transport_unavailable` | The connection to the broker or to Codex was lost | Reconnect, inspect the task, then `codex_resume` |
| `native_protocol_incompatible` | Codex sent a message this version cannot handle safely | Check `codex_status` and `codex_history` |
| `internal_error` | An unexpected failure inside the server | Inspect the task before repeating anything; look at `broker.log` |

**Codex refused or lacks a feature**

| Code | Meaning | What to do |
|---|---|---|
| `native_rejected` | Codex refused the request. `native_code` has its error number | Read the message and the task |
| `optional_native_capability_unavailable` | The installed Codex does not offer this feature | Use another control, or update Codex |

**Installation and stored data**

| Code | Meaning | What to do |
|---|---|---|
| `broker_identity_mismatch` | The running broker was started by a different version or with different settings | Run `server.py stop` with the same `--config`, then retry |
| `broker_identity_unchecked` | The running broker is too old to say how it was started | Stop it with the version that started it |
| `state_newer_schema` | The database was written by a newer version | Use the newer version. Nothing was changed |
| `state_migration_failed` | The database could not be upgraded. The old data is intact | Keep the state folder and its backup, and report the problem |
| `state_invalid` | Stored data failed a consistency check | Keep the state folder; see [troubleshooting](troubleshooting.md) |
| `duplicate_thread_id` | Two saved tasks claim the same Codex conversation | Keep the state folder and report the problem |
| `incident_preview_mismatch` | `incident-export` was asked to write a file, but the data changed after the preview or the confirmation is missing | Preview again and confirm the new `preview_id` |
| `maintenance_busy` | `prune` was started while a broker is running | Stop the broker with the same `--config`, then retry |
| `prune_plan_limit` | Too many cleanup plans are waiting (32 plans or 16 MiB) | Apply a plan, or wait an hour until old plans expire |
| `backup_destination_exists` | The file given to `backup --destination` already exists | Choose a new file name. The existing file was not touched |
| `prune_plan_invalid` | The cleanup plan is unknown, older than one hour, already applied, or the data has changed since | Make a new plan |
| `maintenance_failed` | Cleanup failed. `applied` says whether anything was removed | Keep the state folder and make a new plan |
| `bridge_error` | Any other refusal | Read the message |

## Projects, status and models

| Tool | Required | Optional (default) | Returns |
|---|---|---|---|
| `codex_status` | none | `requests_cursor`, `requests_limit` (50) | Codex version, sign-in type (no credentials), the state database's schema version, pending requests, event cursor. Starts Codex if needed |
| `codex_projects` | `path` for `select`/`create` | `action` (`list`), `offset` (0), `limit` (100) | Previously selected projects, or the selected/created project with its `project_id` |
| `codex_models` | none | `cursor`, `limit` (100) | Model catalog page: model IDs, supported and default reasoning efforts, service tiers, `nextCursor` |
| `codex_usage` | none | none | Account rate limits and remaining allowance. Read-only |
| `codex_capabilities` | `task_id` | none | Collaboration modes, permission profiles and direct-input capability for a task |
| `codex_skills` | `task_id` | `force_reload` (false) | Skills available in the task's project |

### Compatibility information

`codex_status` and `codex_capabilities` also report how well the installed Codex is known to work with this server:

| Field | Meaning |
|---|---|
| `compatibility` | `version`, `status` and `reason`. `status` is `tested` for a Codex version that the project is tested with, and `untested` for a prerelease or a newer version. An untested version still runs, because the permission checks are verified for every conversation in any case |
| `minimum_codex_version`, `tested_codex_versions` | Currently `0.167.0` and `["0.167.0"]` |
| `supported_mcp_revisions` | The MCP versions the server speaks |
| `unavailable_optional_methods` | Optional Codex features that this Codex has refused during the current connection. The server does not call them again until Codex is restarted |
| `os_process_check` | The [check for leftover processes](#leftover-processes-on-linux): `enabled`, `available` and a `reason` (`ok`, `not_linux`, `proc_unreadable` or `disabled`) |
| `ingress_backlog` | Events from Codex that are waiting to be stored: `events`, `bytes`, `oldest_age_seconds`, `dropped_events` |

If a tool needs an optional Codex feature that the installed Codex does not have, it fails with `optional_native_capability_unavailable: METHOD is unavailable in VERSION`, followed by what to do next. Nothing is left half-started. Use another control, or install a tested Codex version and run `server.py stop`.

`codex_projects` with `create` makes an ordinary directory. It does not clone repositories, run `git init` or install dependencies; worktrees are made with [`codex_worktrees`](#worktrees). A model listed in the catalog may still be unavailable to your account; such a failure is reported by the turn.

## Tasks

| Tool | Required | Optional (default) | What it does |
|---|---|---|---|
| `codex_start` | `prompt`, `request_id`, and exactly one of `workdir` or `project_id` | `model`, `effort`, `access` (`write`), `approval_mode` (`inherit`), `collaboration_mode` (`default`), `service_tier`, `name`, [rich input](#rich-input) | Creates a persistent conversation and starts the first turn. Returns immediately |
| `codex_send` | `task_id`, `prompt`, `request_id` | `mode` (`follow_up`), `expected_turn_id`, `model`, `effort`, [rich input](#rich-input) | Starts a follow-up turn, or with `mode: "steer"` adds input to the running turn |
| `codex_interrupt` | `task_id`, `expected_turn_id`, `request_id` | none | Asks Codex to stop the turn. Poll until the status is `interrupted` |
| `codex_resume` | `task_id`, `request_id` | none | Reloads a saved conversation after a restart. Never replays a prompt |
| `codex_list` | none | `offset` (0), `limit` (50), `view` (`default`) | Tasks, newest first |
| `codex_get` | `task_id` | `view` (`default`, `compact`, `result`, `artifact`, `reconciliation` or `permissions`), `turn_id`, `result_id`, `artifact_id`, `offset`, `limit`, `requests_cursor`, `requests_limit` (50), `subagents_offset` (0), `subagents_limit` (50), `claims_offset` (0), `claims_limit` (100) | Status, latest summary, changed files, last 100 commands with exit codes, plan, usage, pending requests, subagents, and what still reserves the project |

`summary` is the latest message written by Codex and can be an interim message while work is running. A command's exit code 0 does not by itself prove that all requested tests passed; read the output and the final report.

### Fields in a task record

| Field | Meaning |
|---|---|
| `turn_id`, `turn_status` | The latest turn and its state in Codex: `inProgress`, `completed`, `failed`, `interrupted`, or `null` if no turn is known |
| `summary`, `files`, `commands`, `plan`, `diff`, `error` | Results of the current turn only. They are cleared when a new turn begins |
| `diff_turn_id` | The turn that the stored diff belongs to |
| `active_settings` | Settings changed for the running turn only, with the `turn_id` they apply to. Empty again on the next turn |
| `usage`, `usage_scope` | Token usage. `usage_scope` is `"cumulative"`: the numbers cover the whole conversation, not one turn |
| `evidence_complete`, `evidence_gaps` | `false` with a list of reasons, such as `["recovery"]` or `["command_limit"]`, when the files, commands or diff may be incomplete, for example after a restart |
| `workspace_reserved`, `workspace_claims` | Whether this task family still reserves its project, and why. See [Why a project stays busy](#why-a-project-stays-busy) |
| `background_observation` | What Codex last reported about the task's background terminals: whether any were `active`, and a short preview of their commands with secrets removed |
| `process_state` | Present when work was running at the moment the server lost contact with Codex. See [After a restart or an update](#after-a-restart-or-an-update). On Linux it also carries `os_process_check`, the latest count of [leftover processes](#leftover-processes-on-linux), and can then appear without any warning |
| `process_scan` | For a task taken over from an older database: whether its last turn was checked for unfinished commands (`complete`) and how many were found (`unfinished`). A scan that could not finish keeps the project reserved |
| `revision` | A counter that increases every time the record changes |

### Turn results and artifacts

`codex_get` with `view: "result"` returns what is known about one turn as a single record, and says where each item came from. Use it for a final report instead of collecting the facts from events.

```json
{"task_id": "TASK", "view": "result"}
```

Without `turn_id`, the current turn is used. The record is returned in `result`:

| Field | Meaning |
|---|---|
| `result_id`, `revision`, `sha256` | Identify this version of the record. A new version is made only when the evidence has changed |
| `turn_id`, `status` | The turn and its state |
| `agent_claims` | What Codex said it did. A claim is not proof |
| `observed_commands` | The commands Codex reported, with their exit codes |
| `reported_changes` | The file changes Codex reported |
| `artifacts` | References to large content and to changed files |
| `limitations` | Notes on what the record cannot show |
| `evidence_completeness` | `complete`, and the `gaps` when something is missing |
| `counts`, `next_offset` | Totals, and where the next page starts |

Each observed item has a `source` with the sequence number of the journal event it was taken from. The lists are paged together with `offset` and `limit` (50 by default, 200 at most). When you read more than one page, pass the `result_id` of the first page, so that all pages come from the same version.

**Artifacts.** Content larger than 16 KiB is not put into the record. The record holds a reference instead: `artifact_id`, `kind`, `sha256`, `size_bytes` and, for a file, its relative `path`. Read the content with:

```json
{"task_id": "TASK", "view": "artifact", "artifact_id": "ARTIFACT", "offset": 0}
```

The result has an `artifact` object with `data` (base64), `sha256`, `size_bytes` and `next_offset`. Call again with `offset` set to `next_offset` until it is `null`. One call returns 65,536 bytes by default and 262,144 at most.

| Kind | Meaning |
|---|---|
| `captured` | Bytes saved when the turn finished. They never change |
| `live_file` | A changed file that is read from the project when you ask for it. If the file has changed since the turn, the call fails with `artifact_version_mismatch` and returns nothing, so you never get different content under the same reference |

- Only files that Codex reported changing in that task can become artifacts. There is no argument for a file path.
- Text is redacted like every other result. `content_redacted` tells you when that happened, and `sha256` is the hash of the bytes you receive.
- One artifact holds at most 32 MiB. Larger content is marked `content_omitted: true`, and the record is marked incomplete.
- For a turn that finished before the update to 0.2.0, the record is built the first time you ask for it. It is marked incomplete with the gap `historical_evidence`, and no file content is captured for it.
- After old data has been [cleaned up](installation.md#backing-up-and-cleaning-up), a result can be gone. The call then fails with `result_expired`.

### The reconciliation report

After a restart, a lost reply or an `uncertain` status, the question is always the same: what was asked for, what really happened, and what is unknown? `codex_resume` now answers it. Its result has a `reconciliation` report, and `codex_get` with `view: "reconciliation"` returns the last saved report again without contacting Codex.

```json
{"task_id": "TASK", "view": "reconciliation"}
```

| Field | Meaning |
|---|---|
| `native` | What Codex itself reported when it was last asked: the state of the conversation, the latest turn, the goal, the queue and background terminals. `background_terminals` is `null` and `background_terminals_checked` is `false` when the terminals could not be checked, for example for commands started before a restart. `current_backend_background_terminals` counts only what the Codex process running now has started |
| `operations` | The recent calls that changed something, at most 20. Each has `requested`, an `acknowledgement` (`acknowledged`, `pending`, `unknown`, `rejected` or `failed`) and `effect_observed`. When an effect was observed, `effect_source` says where: `native_queue` (the instruction is in Codex's queue) or `turn_record` (the call is linked to a turn that Codex reported; `effect_turn_status` gives that turn's status) |
| `os_process_check` | On Linux: how many [leftover processes](#leftover-processes-on-linux) the server sees in the project right now (`processes_total`, up to ten `pids`). `next_steps` then has an entry with `action: "stop_processes"` |
| `rebuilt` | What `codex_resume` filled in again from Codex's history, such as the list of commands |
| `projection_sources` | Where each restored part came from: the server's own journal or Codex's history |
| `unknown` | What could not be established, for example `operation_acknowledgement_unknown` or `journal_native_disagreement` |
| `next_steps` | What is safe to do now, each as a ready-made tool call with a reason |
| `report_stale` | `true` when the task has changed since the report was made. Call `codex_resume` for a fresh one |

Read it this way:

- An acknowledgement means that Codex accepted the call. It does not mean that the work was done; `acknowledgement_proves_completion` is always `false`.
- Nothing is ever repeated for you. `automatic_retry` and `replay_permitted` are always `false`. If an operation is `unknown`, check the task and send new work with a new operation key.
- `effect_observed: false` means that no link between the call and a result could be established. It does not mean that nothing happened.
- When the journal and Codex's history disagree, both are kept and the disagreement is listed under `unknown`. A missing diff is not invented.
- Very large stored arguments or results are left out of the report (`requested_omitted`, `cached_result_omitted`), and `unknown` then contains `operation_record_budget`.

## Progress and history

| Tool | Required | Optional (default) | Returns |
|---|---|---|---|
| `codex_events` | none | `task_id`, `after` (0), `limit` (100), `wait_seconds` (0, max 25), `view` (`default`, `compact` or `outcome`), `task_ids`, `cursors`, `tasks_offset`, `include_descendants` (true), `requests_cursor`, `requests_limit` (50), `event_seq`, `event_offset` | Journal events after the cursor, the next `cursor`, `has_more`, and the current pending requests |
| `codex_history` | `task_id` | `kind` (`turns` or `items`), `cursor`, `limit` (50), `turn_id` | Codex's own persisted history, for recovery after an outage |
| `codex_diff` | `task_id` | none | The diff and file list that Codex reported for the current turn, with `diff_turn_id` (not an independent Git diff). A diff from an earlier turn is never shown as the current one |

**Turns inside a turn.** Codex runs an inline code review as a turn inside the turn that asked for it. The task, its results and artifacts, plan steps and budgets always use the outer turn, so a review counts as one turn and finishes when the outer turn finishes. In `codex_history`, each turn keeps Codex's own `id` and `rootTurnId` and adds `logical_turn_id`: the outer turn's ID when that turn belongs to the same task, otherwise the turn's own `id`. Pass `logical_turn_id` to `codex_revert`.

A subagent's turns also name the parent's turn as their `rootTurnId`. That turn belongs to another conversation, so the subagent's turns count and finish on their own. In one rare case the server cannot tell which turn is the review: a review inside a subagent, where the chain of turns leads back to the parent. The task then stays busy and its project stays reserved, and calls are refused with `Inline review turn identity is unresolved; use codex_resume, then codex_history`. Call `codex_resume`, then read `codex_history`. The server never guesses, and it does not use a timer.

How to use event cursors:

- `after` is exclusive. Process a page before saving its `cursor`. Fetch again immediately while `has_more` is true.
- A cursor from a task-filtered call is not valid for unfiltered calls, and vice versa.
- If old events were [cleaned up](installation.md#backing-up-and-cleaning-up) and your cursor points into the removed range, you first get the events that still exist before it and then the error `cursor_expired`. Its `resume_after` is the cursor to continue from.
- Events include Codex's messages, completed items, command output, file changes, plans, usage, automatic-review results and `bridge/*` lifecycle events. Raw private reasoning is never included.
- Pending requests are part of every page, so missed notifications cannot hide them. At most `requests_limit` are included. If `pending_requests_has_more` is true, call again with `requests_cursor` set to `pending_requests_next_cursor`.
- If Codex produces output faster than it can be stored, some streamed text and output deltas are skipped, and a `bridge/event_gap` event says how many (`dropped_events`). Approvals, completed items and final results are never skipped. `codex_history` has the complete output.
- A `bridge/protocol_error` event means that Codex sent a message this version does not understand. The affected task becomes `needs_resume`. Call `codex_resume` and check `codex_history` before continuing.
- An event that is too large for one page arrives in pieces. The page then contains `fragment`, `fragment_complete: false`, `event_seq` and `next_event_offset`. Keep `after` unchanged and call again with that `event_seq` and with `event_offset` set to `next_event_offset`, until `fragment_complete` is true. Only then use the new `cursor`.

### Compact view

`codex_get`, `codex_list` and `codex_events` accept `view: "compact"`. It returns the same facts without the large parts, so routine polling uses much less of the main agent's context.

| Tool | Left out | Added instead |
|---|---|---|
| `codex_get`, `codex_list` | `summary`, `files`, `commands`, `plan`, `diff`, `error`, `usage` | `counts` (the number of `files`, `commands` and `plan` steps), `has_diff`, `has_error` |
| `codex_events` | The body of each event (`data`) | `data_omitted: true`, and `content_bytes` with the size of the stored body |

Status, turn IDs, evidence flags, pending requests, subagents and project reservations are still included. To read what was left out, repeat the call without `view`. Compact `codex_events` cannot be combined with `event_seq` or `event_offset`.

A good pattern is to poll with `view: "compact"` and to read the full record once, when the turn has finished.

### Watching several tasks

`codex_events` with `view: "outcome"` reports what has *changed* for up to 50 tasks in one call, instead of replaying every event. Use it when the main agent supervises several tasks, or when it only needs to know when to look.

```json
{"view": "outcome", "task_ids": ["TASK"], "wait_seconds": 25}
```

The result has one entry per task in `outcomes` (status, turn, counts, whether there is a diff or an error, and whether the evidence is complete), the current `pending_requests`, and a `cursors` object. Pass that object back unchanged in the next call. The call then returns as soon as one of the tasks changes, or after `wait_seconds`:

```json
{"view": "outcome", "task_ids": ["TASK"], "cursors": {"TASK": "CURSOR_FROM_THE_LAST_RESULT"}, "wait_seconds": 25}
```

- Start without `cursors`. The first result describes the current state.
- Without `task_id` and `task_ids`, all tasks are reported, newest first, 50 per page. Continue with `tasks_offset` set to `tasks_next_offset`.
- `result_ref` in an entry is the ready-made `codex_get` call that returns the [turn result](#turn-results-and-artifacts).
- `suggested_wait_seconds` tells you how long to wait before the next call: 0 while a request is pending, 1 while a task is running, otherwise 5.
- Streamed text and command output do not count as a change. They are still available through the normal event replay, which keeps its own numeric `after` cursor. In this view, `after`, `event_seq` and `event_offset` are not used.
- A cursor also covers the project reservations of the task's family. When a claim of the task or of a related task is added, changed or released, the next call returns at once.
- An outcome cursor belongs to one task and one database. After a [cleanup](installation.md#backing-up-and-cleaning-up), or if it is used for another task, the call fails with `cursor_expired`. Start again without that cursor.

## Questions and approvals

| Tool | Required | Optional (default) | What it does |
|---|---|---|---|
| `codex_requests` | none | `task_id`, `cursor`, `limit` (50) | Lists pending questions and approvals, including those from subagents. Returns `requests`, `next_cursor`, `has_more` and `total` |
| `codex_respond` | `request_id`, `response` | `proposal_hash` | Sends a validated answer. See [response formats](#response-formats-for-codex_respond). `proposal_hash` is needed only to accept a file approval |
| `codex_review_request` | `request_id` | `offset`, `limit` (65536) | Shows the request to the human through the client's form prompt. Without prompt support, it returns `status: "pending"` and the request must be reviewed in a terminal. With `offset`, it returns the full request in pages instead; see [Large requests](#large-requests) |

Each pending request also carries `pending_request_id` (the same value as `id`), `content_redacted` and `redaction_count`. `content_redacted: true` means that the server replaced something that looked like a credential with `[REDACTED]` before showing the request; `redaction_count` says how many values were replaced. The request that Codex acts on is not changed.

### What a request would allow, and who decided

Every pending request carries a `permission_summary`, and its full form (through `codex_review_request`) a `permission_view`. They state in normalized form what the request asks for:

| Field | Meaning |
|---|---|
| `sources`, `destinations`, `filesystem` | The paths involved, each with the path as requested, the resolved path (`normalized`) and `in_project` |
| `network` | Whether network access is requested, and for which host: `scope` is `none`, `all_hosts` or `specified_host` |
| `scope` | How long the permission would last. Only single requests and single turns exist; `session_grant` is always `false` |
| `requested_cwd`, `cwd` | For a command: the folder it asked to run in, and that folder resolved (`null` if it cannot be resolved) |
| `scope_verified`, `unknown` | Whether the server could confirm the conversation's project, and anything it could not interpret. A command folder that cannot be resolved or lies outside the project is listed as `command_cwd_unresolved` or `command_cwd_outside_project`, and `scope_verified` is then `false` |

This is an explanation. It grants nothing, and Codex still enforces its own rules.

`codex_get` with `view: "permissions"` shows the settings of a task (what was requested, what is in effect, and what a subagent inherited from its parent) and the task's **decision receipts**:

```json
{"task_id": "TASK", "view": "permissions"}
```

A receipt is written for every answer to a request. It records the request, the hash of the proposal that was decided on, the decision, the time, and `decision_source`:

| `decision_source` | Who decided |
|---|---|
| `host_elicitation` | A person, in the client's prompt |
| `local_terminal` | A person, with `server.py review` |
| `operator_relay` | The main agent, because `approval_relay` is enabled |
| `main_agent_denial` | The main agent denied the request |
| `main_agent` | The main agent answered a question addressed to it |
| `codex_reviewer` | Codex's own reviewer (*Approve for me*) |
| `human` | A person, through a client that does not say which way |

A receipt contains no answer text and no file content. It applies to that one request and cannot be reused. `delivery` is `sent` when the answer was written to Codex, which is not proof that Codex applied it. `codex_respond` returns the receipt of its own call.

**File changes with hidden values.** When something in a patch looked like a credential, the copy shown for review has `[REDACTED]` in its place, and the request is marked with `content_redacted`, `person_review_required: true` and a `review_warning`. Such a patch can be accepted only by a person, in the client's prompt or with `server.py review`. Both show the warning and the number of hidden values. The main agent cannot accept it, even with `approval_relay` enabled; the call fails with `person_review_required`. It can still deny it.

Positive approvals need the human prompt, the terminal (`server.py review REQUEST_ID`) or the operator setting `approval_relay = true`. Denials and answers to `parent_question` can always be sent by the main agent.

### Large requests

A small request is returned in full. A request larger than 4 KiB, such as a big patch, is returned as a summary: its ID, kind, sizes, a 512-character `preview`, `details_truncated: true` and, for file approvals, `proposal_revision` and `proposal_hash`.

To read the whole request, call `codex_review_request` with `offset: 0`. The result has `detail` (one piece of the request as JSON text), `next_offset`, `detail_total_chars`, `detail_sha256` and `detail_complete`. Call again with `offset` set to `next_offset` until `detail_complete` is true. Called this way, the tool never opens a prompt.

The human prompt and the terminal review always load the complete request first. If it is too large to show in the client's prompt, the prompt offers no way to accept, and the request must be reviewed in a terminal.

## Settings and subagents

| Tool | Required | Optional (default) | What it does |
|---|---|---|---|
| `codex_settings` | `task_id`; `request_id` for changes | `scope` (`get`, `next`, `current`), `expected_turn_id`, `model`, `effort`, `approval_mode` (`ask`/`approve_for_me`), `collaboration_mode` (`default`/`plan`), `service_tier`, `access` (`read`/`write`), `budget` | Reads or changes settings. `current` applies to the running turn only and cannot change access or collaboration mode. `budget` sets the [family budget](#family-usage-and-budgets) (root task, `scope: "next"`) |
| `codex_delegate` | `task_id`, `prompt`, `request_id` | `expected_turn_id` (required if the parent is running; refused with `stale_turn` if that turn has ended), `contract` | Asks the parent to spawn or control its native subagents. `contract` is described under [structured questions and delegation contracts](#structured-questions-and-delegation-contracts) |
| `codex_subagents` | `task_id` | `cursor`, `limit` (50), `refresh` (true), `archived` (false), `offset` (0) | Discovers and verifies native descendants and returns their task IDs. To page through children that are already known, use `refresh: false` with `offset` and the returned `next_offset` |

`approval_mode` values: `inherit` (start only; keep Codex's configured reviewer), `ask` (human reviewer), `approve_for_me` (Codex's automatic reviewer `auto_review`). `codex_settings` refuses `inherit` with `invalid_arguments`; choose one of the other two there. Service tiers must be offered by the chosen model; `default` clears a tier.

### Structured questions and delegation contracts

A question from Codex and a delegation to a subagent are normally plain text. Both can carry an optional structure, so that the two sides agree on exactly what is being decided or delivered. It is the same structure in both directions:

| Field | Meaning |
|---|---|
| `version`, `id` | Always `1`, and a short label chosen by the author (letters, digits, `.`, `:`, `_`, `-`) |
| `goal` | What is to be decided or achieved |
| `constraints`, `deliverables`, `verification` | Short lists of text: limits to respect, what to hand over, how it will be checked |
| `options` | For a question: the choices, each with an `id` and a `label` |
| `evidence` | References to [turn results or artifacts](#turn-results-and-artifacts) of the same task family: `task_id`, `turn_id`, `result_id`, `sha256` and optionally `artifact_id`. An artifact must be one of that result's own artifacts. Content is never copied in |
| `ttl_seconds` | Optional lifetime, 60 seconds to 7 days. The server works out the deadline and reports it as `expires_at` |

The server adds where the structure belongs (task, turn, request) and a `context_hash`, and stores it. The whole structure is limited to 32 KiB.

**A delegation with a contract** uses `contract` on `codex_delegate`:

```json
{"task_id": "TASK", "prompt": "Review the parser changes.", "request_id": "delegate-review-1", "contract": {"version": 1, "id": "parser-review", "goal": "Find defects in the parser changes", "deliverables": ["A list of defects with file and line"], "verification": ["Each defect has a failing test or a reproduction"], "ttl_seconds": 3600}}
```

The task record then shows `delegation_contract` with its `delivery` state (`acknowledged` with the turn it was sent to, `rejected`, `uncertain`, ...). If the contract has expired before it could be sent, the call fails with `decision_expired` and nothing is sent. A contract that expires later does not stop work that is already running; it is only reported as expired. A contract belongs to the subagent it was sent to. Other subagents that Codex starts on its own, and that the server adopts later, have none.

**A question with options.** When Codex attaches a structure to a question, the pending request has a `decision_summary` (its `id`, `context_hash`, a preview of the goal, `expired` and `typed_response_allowed`). The full structure is in `decision_envelope` of the complete request. The main agent can answer in plain text as before, or choose an option:

```json
{"request_id": "PENDING_ID", "response": {"text": "Use option B; it keeps the public API.", "decision": {"version": 1, "id": "ID_FROM_THE_REQUEST", "context_hash": "HASH_FROM_THE_REQUEST", "option_id": "b"}}}
```

- `id` and `context_hash` must be the ones from the request, and `option_id` one of the offered options. Otherwise the answer is refused with `decision_mismatch` and the question stays open.
- After the deadline, choosing an option is refused with `decision_expired`, so that a decision is never applied late. A plain-text answer is still possible, for example to tell Codex that the decision window has passed.
- This structure is guidance only. It can be used on questions to the main agent and on delegations, never on approvals, permissions or questions for the user, and it grants nothing.

### Family usage and budgets

Every task family (a root task and its subagents) has a `family_usage` record. It is in `codex_get`, in the turn result and the reconciliation report, and in short form in the [outcome view](#watching-several-tasks). The existing `usage` field, Codex's own token counter for one conversation, is unchanged.

| Field | Meaning |
|---|---|
| `turns` | Turns started through this server, including queued instructions once Codex has taken them, delegations and goal activations |
| `native_turns` | Turns Codex started on its own, for example to continue an active goal |
| `active_seconds` | Time during which a turn was running, summed over the family. `null` when it is not known; `known_active_seconds` then has the part that is known |
| `tokens` | `input`, `output` and `total`, summed from Codex's own usage reports. For a fork or an imported session only the usage after the fork or import counts |
| `output_bytes` | What the server stores for the family: event journal and captured artifacts. A cleanup reduces it |
| `children`, `reserved_turns`, `reserved_children` | Subagents, and slots held for work that has been admitted but not yet confirmed |
| `complete`, `gaps` | `false`, with the reasons, when a figure is not fully known |

The reasons in `gaps` say what is missing, for example `not_counted_before_upgrade` (usage from before this version), `active_time_unknown` (after a restart), `tokens_not_reported`, `usage_delayed`, `inherited_token_baseline_unknown` (a fork whose starting point is not known yet) or `queued_turn_unconfirmed`. Usage is never estimated in money.

**A budget** limits a family. Set it on the root task:

```json
{"task_id": "TASK", "scope": "next", "request_id": "budget-1", "budget": {"max_turns": 20, "max_active_seconds": 3600, "max_tokens": 2000000}}
```

`{}` removes the budget, and `scope: "get"` shows it. The settings file can give every new family a default budget (see [configuration](configuration.md)), which is also how a family gets a budget for its very first turn.

| Field | Kind | What happens |
|---|---|---|
| `max_turns` | Hard | A turn beyond the limit is refused before anything is sent to Codex |
| `max_children` | Hard | A delegation beyond the limit is refused. Subagents that Codex creates on its own cannot be prevented; they are counted and reported |
| `max_active_seconds` | Soft | Checked by a timer while a turn runs |
| `max_tokens` | Soft | Checked when Codex reports usage |
| `max_output_bytes` | Soft | Checked as output arrives |

All are positive integers and optional. Without a budget there is no limit.

**When a hard limit is reached**, the call fails with `budget_exceeded`, naming the limit and the observed value:

```text
Family budget max_turns reached: observed 1, limit 1. Use codex_settings on the root task to raise the limit or set budget={} to clear it
```

**When a soft limit is crossed**, the server interrupts the running turn once, in the normal way, and records a `budget_stop` on the task:

```text
Cooperative budget stop; may overshoot. Commands already executed are not rolled back
```

A soft limit is checked at certain points, so the family can go beyond it before the stop takes effect. Output that arrives in one large piece, for example, is counted only once it has arrived. Nothing that was done is undone. After a stop, new work in the family is refused with `budget_exceeded`. Everything needed to look and recover still works: reading, `codex_resume`, interrupting, answering or denying pending requests, stopping background terminals, archiving, deleting, and changing the budget. Raising or removing the limit lifts the stop.

Turns that Codex starts by itself to continue an active goal are not admitted by the server, so a hard turn limit cannot prevent them; they appear in `native_turns`, and the soft limits still apply. Starting a goal on an idle conversation counts as a turn. A native goal's own token budget (`codex_goal`) is reported in `native_goal_budgets` and is never changed by the server.

## Conversation controls

All of these need `task_id` and `request_id`. Most require the task family to be idle.

| Tool | Extra arguments | What it does |
|---|---|---|
| `codex_review` | `target` (`uncommittedChanges`, `baseBranch` + `branch`, `commit` + `commit`, `custom` + `instructions`) | Starts a native code-review turn |
| `codex_fork` | `name`, `last_turn_id` (optional) | Copies the conversation history into a new task. Does not start work |
| `codex_rename` | `name` | Renames the conversation |
| `codex_archive` | `action` (`archive` or `unarchive`) | Archives or restores the conversation and its subagents |
| `codex_delete` | `confirm_thread_id` (must equal the task's `thread_id`; for a task that [never reached Codex](#a-task-that-never-reached-codex), its task ID) | Permanently deletes the native conversation and subagents. The bridge's audit records remain. Also removes a task whose conversation [no longer exists in Codex](#a-task-whose-conversation-is-gone) |
| `codex_compact` | none | Compacts the conversation history. May use model quota |
| `codex_revert` | `before_turn_id` (a `logical_turn_id` from `codex_history`), `confirm_history_only: true` | Removes that turn and all later turns from the conversation. **Does not revert files** |
| `codex_queue` | `action` (`list`, `add`, `update`, `delete`, `reorder`, `start`), `prompt`, `queued_id`, `ordered_ids`, `cursor`, `limit`, rich input | Manages queued instructions. `list` needs no `request_id`. An idle task may consume an added entry immediately |
| `codex_terminals` | `action` (`list`, `terminate`, `clean`), `process_id`, `cursor`, `limit` | Lists or stops the task's background processes. `list` needs no `request_id` |
| `codex_goal` | `action` (`get`, `set`, `clear`), `objective`, `status`, `token_budget`, `user_requested` | Native persistent goal. Changes require `user_requested: true` and must come from an explicit user request |
| `codex_sessions` | `workdir` or `project_id`; `action` (`list`, `import`), `thread_id`, `confirm_stopped`, `cursor`, `limit`, `archived` | Lists or imports existing Codex CLI conversations from exactly this project. Import requires `confirm_stopped: true` |

## Worktrees

To let two tasks work on the same repository at the same time, each needs its own folder. A Git worktree is such a folder: a second checkout of the same repository on its own branch. `codex_worktrees` prepares one, registers it as a project, and turns the result of a task into a patch you can review. It never merges, pushes, deletes unsaved work or forces anything.

The tool is off by default. Turn it on with `worktrees = true` in the [settings file](configuration.md) and restart the broker. Git 2.36 or newer is needed.

| `action` | Arguments | What it does |
|---|---|---|
| `plan` | `repo` (absolute path) or `project_id`; `name`; `base` (`HEAD`) | Shows what would be created: folder, branch `codex/NAME`, base commit, shared Git directory, and a `plan_id`. Changes nothing |
| `create` | the same as `plan`, plus `plan_id` and `request_id` | Creates the worktree if nothing has changed since the plan, and returns its `project_id` |
| `inspect` | `project_id` or `path` | State of the worktree: branch, head, unsaved changes, the tasks using it, unfinished operations |
| `handoff` | `task_id` of a task in the worktree whose turn has finished | Stores the changes since the base as a patch artifact and returns a reference to it |
| `cleanup` | `project_id`, `request_id`; `keep_branch` (false) | Removes the worktree and its branch |

The folder is created next to the repository, as `REPO.worktrees/NAME`, unless `worktree_root` names another place. The new project is used like any other: `codex_start` with its `project_id`. Two worktrees of the same repository are different projects and can run at the same time.

**Codex cannot use Git inside a worktree.** A worktree keeps its Git data in the main repository's `.git` folder, which is outside the worktree's project and therefore outside Codex's sandbox. Codex can edit files and run tests, but `git status`, `git diff`, `git log`, `git add` and `git commit` fail there with "not a git repository". Codex offers no way to give one task read-only access to that folder, and the server does not widen the sandbox. The deliverable is the patch from `handoff`; `plan` and `inspect` list the shared folder under `shared_resources` with `task_access: "not_granted"`.

**The handoff patch** contains every change since the base, including new files that Git does not ignore (so add a `.gitignore` for build output). It is limited to 1 MiB and stored as an artifact of the task's latest turn. Read it with `codex_get`, `view: "artifact"`, and the returned `artifact_id`; the content comes base64-encoded. The worktree and its index are not changed by a handoff.

**While a task works in the main checkout**, `create`, `handoff` and `cleanup` are refused, and `inspect` reports the worktree's changes as unknown:

```text
Worktree changes need the main checkout to be idle. Blocking tasks: TASK_ID.
```

This is a safety rule. The server's own Git runs outside the sandbox, and a task in the main checkout can change the repository's Git configuration. To make sure the server never runs a command that such a task has slipped into that configuration, it waits until no task works there. On Linux, leftover sandbox processes in the main checkout count as work too.

**The server's Git is restricted.** It ignores your system and personal Git configuration, disables hooks and the file-system monitor, and always names the worktree's Git directory itself. Repositories that would make Git run commands or read data from elsewhere are refused with a plain message, and you prepare the worktree by hand instead: filters (including Git LFS), included or conditional configuration, sparse checkouts, partial clones, object alternates, and settings that run programs. Symbolic links in the target folder or its parents are refused too.

**Cleanup removes nothing that could be unsaved work.** It is refused while a task uses the worktree, when files are changed, new or ignored, when Git index flags could hide changes, and when files exist inside submodules. If the branch has commits that are not in the base, cleanup removes the folder only with `keep_branch: true`, and the branch stays. If `create` or `cleanup` was interrupted, the folder stays reserved, also across restarts, until `inspect` has shown what is there.

**From a terminal**, the same helper is available as `python3 server.py worktree plan|create|inspect|handoff|cleanup` (see `--help`). It shares its lock with the server, but it cannot see the server's tasks: use it only while no Codex task works in the main checkout.

## Plans

A plan lets the main agent describe a few steps and their order once, for example "analyse two approaches in two worktrees, then implement one, then review it". The server starts each step when the steps before it have finished, within the usual limits, and records the outcome. It never retries anything on its own and never merges, deploys or decides for the main agent.

| `action` | Arguments | What it does |
|---|---|---|
| `submit` | `spec`, `request_id` | Checks the plan, stores it and starts what can start |
| `inspect` | `plan_id` | State of every step, what blocks it, and what to do next |
| `list` | `offset` (0), `limit` (20) | Plans, newest first |
| `cancel` | `plan_id`, `request_id` | Stops starting new steps and interrupts running ones |

A plan (`spec`) has a `name`, `version: 1`, up to 16 `nodes` and `max_parallel` (how many steps may run at once, 1 to 4, default 2):

```json
{"action": "submit", "request_id": "plan-1", "spec": {"version": 1, "name": "parser-fix", "max_parallel": 2, "nodes": [
  {"id": "a", "action": "start", "target": {"project_id": "WORKTREE_A"}, "prompt": "Fix the parser bug with approach A and add tests."},
  {"id": "b", "action": "start", "target": {"project_id": "WORKTREE_B"}, "prompt": "Fix the parser bug with approach B and add tests."},
  {"id": "review", "action": "review", "target": {"node": "a"}, "after": ["a", "b"], "prompt": "Compare with the other approach before you review."}
]}}
```

| Node field | Meaning |
|---|---|
| `id` | A short name: letters, digits, `.`, `_`, `-`, up to 32 characters |
| `action` | `start` (a new task), `send` (a follow-up) or `review` (a code review) |
| `target` | For `start`: `{"project_id": ...}` or `{"workdir": ...}`. For `send` and `review`: `{"node": ID}`, an earlier `start` step whose conversation is used |
| `prompt` | Up to 16 KiB. Must not contain anything that looks like a password, token or key, because the plan stores it until the step runs |
| `after` | The steps that must have succeeded first |
| `priority` | -10 to 10 (0). Higher starts first; a step that waits long gains priority, so none is starved |
| `on_failure` | `stop` (default): when this step fails, no further step of the plan starts. `continue`: only the steps that depend on it are skipped |
| `handoff` | `true` (default): the prompt gets a short list of references to the results of the steps it depends on (task, result and artifact IDs, never their content), which Codex can read with `codex_get` |

**Steps that could run at the same time must not use the same folder.** Two such steps must either use different projects, or one must come `after` the other. The server checks this when the plan is submitted, also for a project named once by ID and once by path. Worktrees from [`codex_worktrees`](#worktrees) are the usual way to give parallel steps their own folder.

**A step succeeds** when its turn has finished in Codex, its subagents have finished successfully and no request is waiting for an answer. Codex accepting the request is not enough. A step fails when its turn fails or is interrupted, or when its family's [budget](#family-usage-and-budgets) stops it.

**Every step runs through the normal rules**: project reservations, the leftover-process check, budgets and `max_tasks`. If a step cannot start yet because its folder is busy, it waits and is tried again on the next change, without being counted as a failure. Questions and approvals from a step's task come to the main agent as for any task, and the plan waits for them.

**When an outcome is unknown**, for example after a lost reply or a restart while a step was running, the step is `uncertain`. It keeps its slot, the steps after it wait, and nothing is started again for it. `inspect` names the task; call `codex_resume` for it, and the plan continues once the task's outcome is known.

A step only counts as started when the server knows which Codex turn is its own: the turn ID Codex returned, or a turn that began after the server sent the step's request. A turn that existed before, in that task or in another task of its family, never counts, and neither does a new turn of a task whose [goal](#settings-and-subagents) or queue could have started it on its own. If the reply with the turn ID was lost in such a case, the step stays `uncertain`; the server does not assume that the latest turn is the step's.

**Ending a plan that stays blocked.** Sometimes `codex_resume` cannot settle a step, because the server never learned which turn belonged to it. Read the task's history yourself, stop the task family, then `cancel` the plan and delete the step's task with `codex_delete`. The step then ends as `cancelled` with the reason `task_deleted`, and the plan can finish. This does not record the step as successful.

Step reasons you may see in `inspect`:

| Reason | Meaning |
|---|---|
| `cancelled_before_dispatch` | The plan was cancelled before the step was sent |
| `cancelled_before_execution` | The step was being sent when the plan was cancelled, and the records show that nothing ran |
| `task_deleted` | The step's task was deleted after the plan was cancelled; its outcome stays unknown |
| `not_started` | The step's task [never reached Codex](#a-task-that-never-reached-codex) |

**Results.** `inspect` shows for every step its `status` (`pending`, `ready`, `dispatching`, `running`, `succeeded`, `failed`, `uncertain`, `cancelled`, `skipped`), the `task_id` and `turn_id`, a `result_ref`, the reason it waits or was skipped, its pending requests, and `next_steps`. The plan's `outcome` is `running`, `blocked`, `succeeded`, `failed` or `cancelled`. A step's task shows `plan_id` and `plan_node_id`.

**Submitting the same plan again** returns the stored plan instead of a second copy. Once a cleanup has removed a finished plan, the same plan cannot run again under its old identity; give it a different `name` to run it as a new plan.

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

### Why a project stays busy

A project is reserved while any task in it still has work that could change files. That can be the case after a turn has finished. `codex_get` lists the reasons in `workspace_claims`:

| Claim `kind` | Held while | Released when |
|---|---|---|
| `turn` | A turn is starting or running | The turn ends, or `codex_resume` confirms from Codex's history that it ended |
| `queue` | A queued instruction is waiting, or an add may have gone through | Codex's queue is empty and nothing was started from it |
| `goal` | A native goal is active | The goal is paused, blocked, complete or cleared |
| `uncertain` | The acknowledgement for an action was lost | `codex_resume` has checked what Codex actually did |
| `background` | Background terminals or unresolved subagents exist, or (on Linux) [leftover processes](#leftover-processes-on-linux) were found | They are gone |

You normally do not have to do anything about claims. When a turn ends, the server asks Codex whether anything can still run in the project (queue, goal, background terminals, subagents) and releases every claim that Codex confirms is finished. While claims remain, it checks again every few seconds. A broker with no open claims makes no such calls.

While a claim is held, a second independent task in the same folder, or in a folder inside or around it, is refused. If a check is still running, the call first waits up to 5 seconds for it. A refusal names what blocks the project:

```text
Another task is active in this project. Use a separate project/worktree or finish it first. Blocking workspace claims: task TASK_ID [background=active]. Inspect codex_get for workspace_claims. For task TASK_ID, use codex_terminals to list, then terminate each process_id or clean. Use codex_resume with a new request_id to re-check native state.
```

The message names the tool that clears each kind of claim: `codex_terminals` for `background`, `codex_goal` for `goal` (change a goal only when the user asks) and `codex_queue` for `queue`. Once the cause is gone, the project is freed without further action.

Claims are stored in the database, so they survive a broker restart. A claim is released only when Codex confirms there is nothing left to run, never after a timeout or on a guess. If the installed Codex lacks the optional feature needed for a check, the claim stays.

### Leftover processes on Linux

Codex runs every command inside a sandbox process. With Codex 0.167.0 on Linux such a process can keep running, and keep writing in the project, after Codex has reported the command as terminated, after the broker was stopped, or when a command detached itself with `nohup ... &`. Codex no longer lists it then.

On Linux the server therefore looks at the process list itself before it frees a project. It looks only for Codex's sandbox processes that belong to your user and whose command line names the project folder, a folder inside it or a folder around it. It never stops a process.

- **While such processes exist, the project stays reserved.** `workspace_claims` shows a `background` claim with `source: "os_process_scan"`, up to ten `pids` and `processes_total`. The refusal says, for example:

  ```text
  1 Codex sandbox process is still running in this project (PID 1234). Codex no longer tracks them. Stop them with kill PID; the project is then freed automatically.
  ```

- **Once they are gone, the project is free without any further call.** The server looks again every 5 seconds at first and less often while nothing changes, at most every 5 minutes. A blocked `codex_start` and `codex_resume` look again at once, so you do not have to wait after stopping a process.
- **A new, independent task is refused even if no task holds the folder**, for example when the processes were started with the Codex CLI or are left over from an older version. The message then ends with "No task accounts for them."
- **Follow-ups in the same conversation are never blocked by this check.** The processes may be that conversation's own, such as a development server it started. A forked or imported conversation counts as new until it has run work in the folder through this server.
- **The restart warning is separate.** `processes_unverified` still needs an explicit `codex_resume`, whatever this check finds.

The check is best effort. It recognises Codex's sandbox by the name of its process, as tested with Codex 0.167.0, and it cannot see a process that left the sandbox. Folder names are compared as written and, if needed, after following symbolic links.

If the process list cannot be read, the server cannot tell, and it keeps the project reserved rather than guess. The claim then has `reason: "process_check_unavailable"` and says so:

```text
The process list could not be read; workspace ownership is kept. Run doctor; set sandbox_process_check = false to turn the check off.
```

Processes that were found before the list became unreadable still count; such a claim has `partial: true`, and its `processes_total` may be too low.

Every count the server reports (`os_process_check` in `process_state` and in the reconciliation report) carries `checked_at`, the time of the scan. When Codex has started or stopped something since then, the count is shown as `reason: "not_checked"` with `processes_total: null`, so an old zero is never presented as current. On macOS and Windows the check does not exist, and nothing in this section applies.

To turn it off, set `sandbox_process_check = false` in the [settings file](configuration.md) and restart the broker. Holds that came from this check are then dropped. You may want that if you often use the Codex CLI yourself in a folder the server also works in, because the CLI's commands hold the folder in the same way.

### After a restart or an update

When the broker stops, crashes or is updated, the Codex process it started ends too, and the new one knows nothing about commands the old one started. The server therefore treats two cases differently.

**Nothing was running.** The task had finished, with no running turn and no background terminal on record. The server then re-checks the task's claims on its own. It only reads what Codex has stored about the conversation: its state, its latest turn, its goal and its queue. It does not load, resume or start anything. If the latest turn has ended and no goal or queued instruction is active, the project is free. This is the normal case after an update from 0.1.x: old, finished tasks need no action.

**Something was running.** The task had a running turn or a background terminal when contact was lost. Those commands may have survived the old Codex process and may still write in the project. Nobody can check that from here, so the project stays reserved with the reason `processes_unverified`, and the task record gets a `process_state`:

| Field | Meaning |
|---|---|
| `previous_backend_unverified` | `true`: commands from before the restart could not be checked |
| `acknowledged` | `false` until `codex_resume` has been called for this task |
| `acknowledged_turn_status`, `acknowledged_event_seq` | Set by that call: how the latest turn had ended, and the position in the task's event history it covers. Anything older does not raise the warning again; a new turn or a new background command does |
| `commands_preview`, `commands_truncated` | Up to five commands that were known to be running, shortened and with secrets removed, and whether there were more |

Look at your system's process list, stop what is left, then call `codex_resume` for that task. Only that explicit call clears the claim. Afterwards the task is `idle`, and `turn_status` shows how the cut-off turn ended, usually `interrupted`. The call records that you have looked (`acknowledged: true`); it is not proof that the processes are gone, and the [reconciliation report](#the-reconciliation-report) keeps `processes_unverified` under `unknown` until the next turn starts. A subagent with such a record keeps its whole family reserved until it is resumed itself. If the task is archived, unarchive it first (`codex_archive` with `action: "unarchive"`), or delete it; the refusal names the task and both ways.

On Linux, `process_state.os_process_check` shows how many leftover processes the server can see in the project, which helps you decide. Zero there is a good sign, not proof: the server sees only Codex's own sandbox processes.

A claim that stays after the re-check carries one of these reasons:

| `reason` | Meaning | What to do |
|---|---|---|
| `processes_unverified` | Commands from before the restart may still be running | Check the process list, then `codex_resume` |
| `conversation_not_loaded` | The latest stored turn did not end | Call `codex_resume` and inspect `codex_history` before sending more work |
| `stored_state_unavailable` | Codex cannot read the stored conversation, for example because it no longer exists | See [A task whose conversation is gone](#a-task-whose-conversation-is-gone) |
| `native_state_unavailable` | The conversation was read, but its goal, queue or terminals were not | Call `codex_resume` to check again |
| `native_state_changed` | The conversation became active while it was being checked | Nothing; the next check decides |

The same applies to subagents stored with the conversation: as long as one of them is not proven inactive, the whole family stays reserved. A check that keeps failing for the same reason is repeated less and less often, from every 5 seconds up to every 5 minutes, and writes nothing while the result does not change. `codex_resume` checks again at once.

### A task that never reached Codex

If the server stops after a task was created but before Codex opened its conversation, the task has no `thread_id` and stays `needs_resume`. Because the server stores the conversation ID before it asks Codex to start any turn, a task without one cannot have run anything. `codex_resume` then ends it without contacting Codex:

| Field | Value |
|---|---|
| `status` | `failed` |
| `native_conversation` | `not_started` |
| `message` | `Codex never started this task; nothing ran.` |

Its project is freed at the same time. To delete such a task, pass its task ID as `confirm_thread_id`; there is no conversation to delete in Codex. For every other task, `codex_delete` still needs the real `thread_id`.

`codex_resume` does not do this while the task's start is still in progress; it then answers `No Codex thread was created for this task` and changes nothing. Wait for the start to finish and check the task again.

If Codex received the request just before the server stopped, an empty conversation without any turns may remain in Codex's own history. The server neither uses nor deletes it.

### A task whose conversation is gone

A conversation can disappear from Codex, for example when its session files were removed. The task can then neither continue nor be resumed. `codex_resume` says so:

```text
This conversation no longer exists in Codex or cannot be read. Use codex_delete with confirm_thread_id=THREAD_ID to free this project
```

`codex_delete` with the task's `thread_id` removes the task and frees its project. Before it does, the server asks Codex twice: the conversation must not be among the loaded ones, and reading it must fail. If Codex still has a loaded conversation that the server cannot assign to a task, the delete is refused and nothing changes; try again later. The result tells you what happened:

| Field | Meaning |
|---|---|
| `native_conversation` | `"deleted"`: Codex deleted the conversation. `"unavailable"`: there was nothing to delete in Codex |
| `native_conversations_unavailable` | How many conversations of the family were unavailable |
| `processes_unverified`, `process_states` | `true`, with the affected tasks and their `process_state`, when work was running before a restart. Check for leftover processes yourself |

In the `"unavailable"` case the server deletes nothing in Codex. If the conversation does still exist there, for example because it was only unreadable for a moment, it stays in Codex's own history. That is why both checks are made twice, once at the start and once immediately before the task is removed.

When Codex itself reports that a conversation was deleted, the task is marked deleted and stops reserving its project in one step. If the restart warning was still open at that moment, it stays on the task's audit record as `process_state.deletion_outcome: "native_deleted_unacknowledged"`. Calling `codex_delete` for that task afterwards records that you have seen it.

The audit records of the task remain until a [cleanup](installation.md#backing-up-and-cleaning-up) removes them.

## Response formats for `codex_respond`

**`parent_question`**

```json
{"request_id": "PENDING_ID", "response": {"text": "Use the existing parser interface."}}
```

If the question came with options, `response` can also carry a `decision`; see [structured questions](#structured-questions-and-delegation-contracts).

**`command_approval` and `file_approval`.** One of `accept`, `decline` or `cancel`, and only if Codex offered it in `availableDecisions`. If `decline` is not offered, use `cancel` to deny (this may interrupt the turn). Session-wide approvals and rule changes are not supported. File approvals are refused if the patch would touch anything outside the project.

```json
{"request_id": "PENDING_ID", "response": {"decision": "accept"}}
```

Accepting a **file approval** also needs `proposal_hash`, copied from the pending request. It identifies the exact patch that was reviewed. If Codex has changed the patch since then, the answer is rejected with `File approval proposal is stale or unknown; refresh codex_review_request and review its proposal_hash`, and the request stays pending for a new review. `decline` and `cancel` need no hash. The human prompt and the terminal review send the hash for you.

```json
{"request_id": "PENDING_ID", "response": {"decision": "accept"}, "proposal_hash": "HASH_FROM_THE_REQUEST"}
```

Every path in the patch must be inside the project: the changed file, the destination of a rename, and a requested `grantRoot`. Symbolic links are resolved first. A patch in a shape that the server does not recognise cannot be accepted.

**`user_input`.** Answer every question ID. Questions marked secret are refused; use Codex's own sign-in flow for credentials.

```json
{"request_id": "PENDING_ID", "response": {"answers": {"choice": {"answers": ["Use the current schema."]}}}}
```

**`permissions`.** Grant a subset of what was requested, for this turn only. `{}` grants nothing. Any answer that grants nothing, such as `{"network": {"enabled": false}}`, counts as a denial and needs no human. Filesystem paths must be inside the project (relative paths are relative to the project); glob and special paths are refused.

```json
{"request_id": "PENDING_ID", "response": {"permissions": {"network": {"enabled": true}}, "scope": "turn"}}
```

**`elicitation`** (forms from external MCP tools):

```json
{"request_id": "PENDING_ID", "response": {"action": "accept", "content": {"choice": "yes"}}}
```

`decline` and `cancel` send no content. For URL-mode requests, send `accept` with `content: null` only after the user has completed the provider's page. Forms with advanced schemas or secret fields are refused and must be handled in Codex directly.

## MCP 2026-07-28 and Tasks

A client that speaks MCP 2026-07-28 gets the same 34 tools. Nothing changes for clients on older versions. Three things work differently on the new version.

**No handshake.** Every request names its protocol version and the client's capabilities. `server/discover` is answered without starting the broker or Codex. Only the stdio transport is supported.

**Approval prompts.** `codex_review_request` returns `input_required` together with a form, and the client sends the person's answer with a second call. The rules are the same as for the prompt on older versions: the client must have declared that it can show forms, and the answer counts only for the request, the revision and the file changes that were shown. A form can be used once and expires after 5 minutes. Nothing written into tool arguments is treated as a person's answer.

**Tasks (optional).** If the client declares the Tasks extension on a call that starts a turn, the result contains an MCP task with its own `taskId`. The calls that can do this are `codex_start`, a follow-up `codex_send`, `codex_review`, `codex_compact`, and `codex_delegate` on an idle task. The `taskId` stands for that one turn. It is not the `task_id` of the Codex conversation.

| Method | What it does |
|---|---|
| `tasks/get` | Returns the state: `working`, `input_required`, `completed` or `cancelled`. While Codex waits for an approval, the forms are included |
| `tasks/update` | Sends the person's answers to those forms |
| `tasks/cancel` | Interrupts the turn, like `codex_interrupt`. The acknowledgement does not mean that the turn has stopped. Keep polling until the state is `cancelled` or `completed` |

- A finished MCP task stays finished. A follow-up turn gets a new `taskId`.
- A turn that failed is reported as `completed` with an error result (`isError: true`, `ok: false`).
- The result of a finished MCP task is the acknowledgement of the call plus the IDs of the turn. Read the output with `codex_get`, as usual.
- Questions that Codex asks the main agent (`parent_question`) are not shown as forms. Answer them with `codex_respond`.
- The server remembers up to 1,024 MCP tasks and forgets a finished one 24 hours later. The Codex conversation is not affected by that.

Not supported on this version: the HTTP transport, other Tasks methods, subscriptions, resources, prompts, sampling and URL forms.

This support follows the published specification and its schema. It has not been tried with a real client that uses this version; see [validation status](development.md#validation-status).

## Limits

| Limit | Value |
|---|---|
| Prompt length | 100,000 characters |
| Page size | 1–200 records |
| Poll wait | 0–25 seconds |
| Event page size | about 1 MiB (a larger event is returned in pieces of 65,536 characters) |
| Turn result page | 2 MiB |
| Outcome page | 2 MiB, at most 50 tasks, 16 KiB per task |
| Content kept inside a turn result | 16 KiB. Larger content becomes an artifact |
| Artifact | 32 MiB. Read in pieces of 65,536 bytes by default, 262,144 at most |
| Tool result | 8 MiB. Longer lists are paged, and oversized fields are shortened and marked `truncated` |
| Pending request shown in full | Up to 4 KiB. Larger requests appear as a summary with a 512-character preview |
| Request detail page | 65,536 characters by default, 262,144 at most |
| MCP input frame | 1 MiB |
| Codex frame / broker response | 16 MiB / 32 MiB |
| Concurrent independent task families | `max_tasks` (default 4) |

## Calling tools from the command line

The CLI uses the same validation and allowlist. This is useful for diagnosis:

```bash
python3 server.py call codex_events --args '{"task_id": "TASK", "after": 0, "wait_seconds": 25}'
```

`--args -` reads the JSON from stdin, which keeps it out of the process list.
