# Changelog

## 0.2.4 — unreleased

Fixes from an independent audit of 0.2.3 (dated 2026-11-23). Stop the broker before you update (`python3 server.py stop`, with the same `--config` if you use one), then restart your MCP client. The state database layout is the same as in 0.2.0.

### Fixed

- **Two clients with different `CODEX_HOME` folders could share one broker.** With the same settings and state folder, the second client's tool calls silently ran against the first client's Codex home (another account, configuration or history). Now the effective Codex home (`CODEX_HOME`, or `~/.codex` when unset or empty) is resolved to a canonical path when the configuration is loaded. It is part of the broker's identity. A client with a different home is refused with `broker_identity_mismatch` before any tool runs. Use separate `state_dir` folders to run different Codex homes at the same time.
- **Sending a message to a stalled Codex process had no time limit.** Previously, a request to Codex had a 30-second limit only for the answer. If Codex stopped reading its input, one stuck write could block every later message without end, including interrupts and approval answers of other task families. Every request to Codex now has one 30-second limit covering waiting to send, sending and waiting for the answer. Answers to Codex's requests and other messages also have a 30-second limit for sending. A timeout before anything is sent returns `native_timeout` with `retryable: true`. A timeout while sending returns `native_timeout` with `uncertain: true`, closes the connection to Codex and recovers as after a disconnect.
- **`codex_subagents` with `refresh: true` did not page the returned `subagents` list.** Following `nextCursor` discovered new native children, but each page returned the local list at `offset` (0 by default). That repeated the first children and gave no prompt to list the rest. Results now include `next_steps` with the exact next call. Call with `refresh: true` and `cursor` until `nextCursor` is `null`, then list every verified child once with `refresh: false` and `offset` / `next_offset` until `next_offset` is `null`.
- **The tool guide gave the wrong stopping rule for multi-page `codex_review_request` reads.** It told callers to repeat reads until `detail_complete` was `true`, which never happens when a request spans more than one page. The guide now says to stop when `next_offset` is `null` and to verify the joined `detail` text against `detail_total_chars` and `detail_sha256`.
- **`incident-export` failed when the selected tasks had more than 1,000 requests.** It failed even with `--limit 1`, and several tasks hit the limit sooner. The error (`Unsupported or malformed structural incident fixture`, "Correct the arguments") could not be fixed by any argument. Now the export keeps at most 1,000 requests across all selected tasks and reports how many it left out.
- **The security guide listed 32 instead of 34 tools and stated redaction too broadly.** It now lists 34 tools and notes that pattern-based redaction applies to text only, while binary file content is stored and returned unchanged. The tool guide notes the same for artifacts.

### Changed

- **Clearer tool descriptions in the MCP catalog.** They now explain the two modes of `codex_review_request` (reading details with `offset` versus opening a human-review form). They state that an active `codex_goal` can keep Codex working and consume usage, and that its `token_budget` is separate from the family budget. They include a minimal valid `codex_plans` spec with an example. They also state the `codex_get` views, selectors and paging units, and how `codex_events` fragment offsets continue. Finally, they explain `images`, `context_files`, `skills`, `output_schema` and `access`, and which arguments `codex_send` refuses when steering.
- **Short notes for less common tool options in the MCP catalog.** `codex_revert` states that it never reverts files, that `before_turn_id` is a `logical_turn_id` from `codex_history`, and that this turn and every later turn are removed. `codex_settings` states that `current` changes only the running turn, `next` changes future turns, and the root-only family `budget` applies at once. `codex_projects` states that `select` registers an existing folder and `create` makes one. `codex_worktrees` explains where `plan_id` comes from, that `handoff` returns a patch artifact, and that `cleanup` removes the managed folder and its branch unless `keep_branch` is `true`. `codex_sessions` explains importing a native Codex CLI conversation and `confirm_stopped`. `codex_terminals` states that `clean` stops all background processes of that task, and `codex_delegate` states that the parent decides whether to use subagents. `codex_respond` states what it answers, that `response` must match the request kind, and that no answer form grants authority. 112 of the 205 tool arguments now have a description (105 before), and none lost one. The catalog stays within its 54 KB limit. No tool, argument, default or result changed.
- **Faster `server.py prune` planning on large histories.** On the test machine with 40,000 old events, making a cleanup plan took about 2.6 s before and takes about 0.2 s now. The time now grows in proportion to the history instead of much faster. The plan and what it removes or keeps are unchanged.
- 1306 offline tests (1266 in 0.2.3).

## 0.2.3 — 2026-11-17

Fix found by the third CI run, on Windows. Includes everything in 0.2.2, 0.2.1 and 0.2.0. Stop the broker before you update (`python3 server.py stop`), then restart your MCP client. The state database layout is the same as in 0.2.0.

### Fixed

- **Windows: a stopped pipe server released its handles only at Python's next garbage collection.** The error that ends a cancelled or timed-out pipe operation formed a reference cycle with the code that raised it, which kept the stopped server, its thread and its locks alive. On Windows these hold system handles: one per stop on Python 3.13 and newer, about eight on Python 3.11. Errors in the pipe client, the broker's connections and the connection to Codex are now released the same way, as soon as they are handled.

### Changed

- The Windows pipe test checks that each stopped server is freed at once, without garbage collection, and still fails if handles grow from one round to the next. New tests on every platform check the same for pipe, broker and Codex-connection errors.
- 1266 offline tests (1229 in 0.2.2).

## 0.2.2 — 2026-11-15

Fixes found by the second CI run, on Windows and macOS. Not released on its own; included in 0.2.3. Includes everything in 0.2.1 and 0.2.0. Stop the broker before you update (`python3 server.py stop`), then restart your MCP client. The state database layout is the same as in 0.2.0.

### Fixed

- A worktree name ending in `.` or `.lock` is refused with `name_invalid` (*The name cannot form a Git branch*) before anything is done in the repository. Git cannot use such a name for a branch; on Windows it was refused later with a misleading `unsafe_config`.

### Changed

- Several worktree and Windows pipe tests assumed Linux behaviour (`\n` line endings, `/` in paths, replaceable hidden files, a handle count taken before Python's thread pool started). They now check the same things on Windows and macOS.
- 1229 offline tests (1221 in 0.2.1).

## 0.2.1 — 2026-11-14

Fixes for Windows and macOS, found by the first CI run of 0.2.0 on those systems. Not released on its own; included in 0.2.3. Stop the broker before you update (`python3 server.py stop`), then restart your MCP client. The state database layout is the same as in 0.2.0; coming from 0.1.1, it is upgraded as described under 0.2.0.

### Fixed

- Windows with Python 3.13 or newer: private folders were refused as unsafe, so the settings, `doctor` and the maintenance commands failed. Python now gives such folders an entry for the folder's owner (*OWNER RIGHTS*), which is accepted once the owner has been checked.
- Windows: worktrees could not be used, because the path Git records for the shared Git folder (`../..`) was refused.
- Windows: `prune` could fail to show its plan for some dates.

### Changed

- Worktree commands start half as many Git processes; Git's version is checked once per Git program instead of before every command. This matters most on macOS and Windows.
- CI runs the tests with `scripts/run_tests.py`, which prints each failure's traceback at once, shows where a test hangs and lists the slowest tests. Each test job may run for 45 minutes instead of 15.
- 1221 offline tests (1208 in 0.2.0).

## 0.2.0 — 2026-11-14

Stability release that follows the audit of v0.1.1. Not released on its own; included in 0.2.3. Stop the broker before you update (`python3 server.py stop`), then restart your MCP client. The state database is upgraded automatically on the next start, after a backup is saved next to it.

### Security

- **A file change with hidden values needs a person.** When the server hides a credential-like value in the copy of a patch shown for review, only a person can accept that patch, in the client's prompt or the terminal review. The main agent cannot, even with `approval_relay` enabled.
- **File approvals check every path.** The destination of a rename and a requested root are now checked as well as the changed file. A patch in an unknown shape cannot be accepted.
- **File approvals are tied to the patch that was shown.** Accepting needs the request's `proposal_hash`. If Codex changes the patch before the answer is sent, the answer is rejected and the request stays open. The prompt and the terminal review pass the hash automatically.
- **Answers are sent only to a verified conversation.** After a restart or reconnect, an approval, a goal or a queued instruction is refused until the conversation has been checked against Codex again (`codex_resume`). Permission paths are resolved against the conversation's verified working directory and checked again right before sending.
- **`codex` is searched only in absolute `PATH` entries**, on every platform. Empty and relative entries such as `.` are skipped. On macOS and Linux they were still used.

### Fixed

- A rename, or another control that waited on Codex, could overwrite a finished turn with an old `running` state.
- A new turn could show the previous turn's diff and current-turn settings.
- A rejected queue add left the task `queued` although nothing was queued.
- A permission answer that grants nothing, such as `{"network": {"enabled": false}}`, needed a human.
- A relative `codex_binary` worked in the client but failed in the broker. It is now rejected with a clear message; use an absolute path.
- An interrupt or a denial could wait behind another project's slow call.
- An active goal or a queued instruction did not keep its project reserved after the turn ended.
- Many large pending approvals could make a response too big for the broker's transport.
- A flood of events from Codex was queued in memory without limit.
- Windows: a client that stopped reading a large reply could block a broker worker indefinitely. Pipe writes now have a deadline and can be cancelled. (Not yet run on Windows; see the validation status.)

### Added

- **`doctor` command.** `python3 server.py doctor` checks the installation without starting anything or using a model: versions, how Codex was found, settings and state folder, local sockets, a running broker and the database. `--json`, `--start-broker` and `--schema` add more.
- **Structured errors.** A failed tool call now returns `ok: false` and an `error` with a stable `code`, `retryable`, `uncertain`, `next_steps` and the IDs involved. Successful results add `ok: true` and `result_version: 1`; their other fields are unchanged. See the [tool reference](docs/tools.md#results-and-errors).
- **Compact view.** `codex_get`, `codex_list` and `codex_events` accept `view: "compact"`, which leaves out long fields and event bodies. Use it for polling to save context.
- **Clearer ID names.** `operation_id` and `pending_request_id` are accepted in place of the two meanings of `request_id`, which keeps working.
- **Turn results.** `codex_get` with `view: "result"` returns one record per turn: what Codex said, the commands and file changes it reported, where each item came from, and whether anything is missing. Large content and changed files are available as artifacts (`view: "artifact"`), read in pieces and checked by hash.
- **Reconciliation report.** After a restart or a lost reply, `codex_resume` returns a report of what was requested, what Codex acknowledged, what could be restored and what is unknown. `codex_get` with `view: "reconciliation"` shows the last report. Nothing is ever repeated automatically.
- **Watching several tasks.** `codex_events` with `view: "outcome"` reports changes for up to 50 tasks in one call, with pending requests and a ready-made reference to each turn result.
- **Permission explanations and decision receipts.** Pending requests state in normalized form which paths and which network access they ask for. Every answer leaves a receipt that records who decided (client prompt, terminal, relay, or Codex's own reviewer) and for which proposal. `codex_get` with `view: "permissions"` shows them.
- **Structured questions and delegation contracts.** A question from Codex and a delegation can carry an optional structure with a goal, constraints, deliverables, options, evidence references and a lifetime. The main agent can answer by choosing an option, bound to the exact question. Plain text keeps working. The structure is guidance only and can never approve anything.
- **`backup` and `prune` commands.** `python3 server.py backup` saves a checked copy of the state database, also while the broker is running. `python3 server.py prune --older-than 30d` shows what old data could be removed; nothing is removed until you apply the plan. Data that a running task, an open approval or an unresolved operation needs is never removed. See [backing up and cleaning up](docs/installation.md#backing-up-and-cleaning-up).
- **MCP 2026-07-28.** The server now also speaks the 2026-07-28 revision over stdio, including approval forms and the optional Tasks extension (`tasks/get`, `tasks/update`, `tasks/cancel`). Clients on older revisions see no change. Checked against the published specification, not yet with a real client. See the [tool reference](docs/tools.md#mcp-2026-07-28-and-tasks).
- **`incident-export` command.** Writes the structure of a task without any content, after a preview, so that a problem can be reported and replayed as a test without sharing prompts or code. See [troubleshooting](docs/troubleshooting.md#sharing-what-happened-without-sharing-your-content).
- Output schemas for all tools on MCP 2025-06-18 and newer.
- Pending requests show `content_redacted` and `redaction_count` when a credential-like value was hidden from the displayed copy.
- Wheel and source-distribution checks in CI (`scripts/release_check.py`), richer package metadata, and a manual-only publishing workflow. The package is still not on PyPI.
- Schema versions and automatic, backed-up upgrades for the state database. A database from a newer version is refused and left unchanged.
- `codex_status` and `codex_capabilities` report the Codex compatibility status (`tested` or `untested`), the supported versions and the event backlog.
- One stable error, `optional_native_capability_unavailable`, when the installed Codex lacks an optional feature.
- Paging for pending requests, subagents and large events; large requests are listed as summaries and can be read in full with `codex_review_request`.
- `codex_get` shows why a project is still reserved (`workspace_claims`) and whether results may be incomplete (`evidence_complete`, `evidence_gaps`).
- **Worktrees for parallel work.** With `worktrees = true`, `codex_worktrees` prepares a Git worktree next to a repository, registers it as a project, hands the result of a task back as a patch artifact and removes the worktree when nothing unsaved is left. The server's own Git runs with hooks and your personal Git configuration disabled, refuses repositories that could make it run programs, and does not touch a repository while a task works in its main checkout. `server.py worktree` offers the same from a terminal. Needs Git 2.36 or newer.
- **Plans.** `codex_plans` runs up to 16 dependent steps (start, follow-up, review) in order, at most four at a time, with priorities and a stop or continue rule on failure. A step whose outcome is unknown blocks the steps after it until it is resolved with `codex_resume`; nothing is retried.
- **Family usage and budgets.** `family_usage` shows turns, active time, tokens and stored output of a task family, and says what is not known. A budget set with `codex_settings` or as a default in the settings file limits turns and subagents strictly, and stops active time, tokens and stored output cooperatively (the running turn is interrupted; it may overshoot).
- **Check for leftover processes on Linux.** Codex 0.167.0 can leave a sandboxed command running after it has reported it as terminated. Before a project is freed or newly taken, the server now looks for such processes and keeps the project reserved while any exist; the refusal and `doctor` name their PIDs. Follow-ups in the same conversation are not blocked. Turn it off with `sandbox_process_check = false`. Not available on macOS and Windows.
- `codex_delete` can remove a task whose conversation no longer exists in Codex, which frees its project. `codex_resume` says so when it meets such a task.
- `scripts/live_supervision.py` checks turn results, artifacts, receipts, structured questions and delegation contracts against real Codex.
- Optional manual CI jobs that check the protocol against a pinned and the latest Codex CLI.

### Changed

- **A project stays reserved while something can still run in it.** Background terminals, an active goal or queued instructions of one task now block a new, independent task in the same folder until they are finished or stopped (`codex_terminals`, `codex_goal`, `codex_queue`). Follow-ups in the same conversation are not affected. In 0.1.1 only a running turn reserved the project.
- **After a restart, a task that was working keeps its project until `codex_resume`.** If a turn or a background terminal was running when the broker stopped or crashed, its commands may still be running, and Codex can no longer see them. The project stays reserved with the reason `processes_unverified` until you have checked and called `codex_resume`. Tasks that had finished are freed automatically, also after the update from 0.1.1. See [Commands that keep running](docs/troubleshooting.md#commands-that-keep-running).
- **A refusal is a finished result.** A call that the server refuses before anything is sent to Codex, for example because the project is busy, is saved with its `request_id` and returns the same error when repeated. It is no longer reported as an operation with an unknown outcome.
- **Multi-action tools reject arguments of another action.** For example, `codex_queue` with `action: "delete"` no longer accepts `prompt`. Before, such an argument was ignored. An argument that only repeats its default value is still ignored.
- The state database is upgraded to layout 11 (budgets, plans), still automatically, with a backup, and without reading the stored history.
- The state database has indexes for the lookups done on every event, so a long task history no longer slows the server down. The database is upgraded automatically, in well under a second even for a large history.
- The tool catalog of 34 tools is kept below 54 KB (40 KB for older MCP versions), checked by a test, so that it does not take up more of the client's context than necessary.
- Task results (`summary`, `files`, `commands`, `plan`, `diff`, `error`) belong to the current turn and are cleared when a new turn begins. `usage` is labelled as cumulative.
- **Inline code reviews are tracked by their outer turn.** Codex runs them as a turn inside a turn; the task, results, plan steps and budgets use the outer turn. `codex_history` turns gain `logical_turn_id`, and `codex_revert` takes that ID. A subagent's turns still count and finish on their own, although Codex links them to the parent's turn.
- **A task that stopped before Codex opened its conversation can be ended.** `codex_resume` marks it `failed` with `native_conversation: "not_started"` and frees its project, without contacting Codex; delete it with its task ID as `confirm_thread_id`. Before, such a task could be neither resumed nor deleted. See [A task that never reached Codex](docs/tools.md#a-task-that-never-reached-codex).
- 1208 offline tests (83 in 0.1.1).

## 0.1.1 — 2026-11-09

Bug-fix release. After updating, run `python3 server.py stop` once so the new broker is started.

### Fixed

- **Windows CI failures.** `server.py stop` (and test teardown) returned as soon as the broker released its lock, while the process could still hold `broker.log` open. Deleting or replacing the state folder then failed with `WinError 32`. `stop` now waits until the broker process has exited.
- **Windows broker could stop accepting connections.** If a client connected and disconnected before the named-pipe listener accepted it (`ERROR_NO_DATA`), the listener thread exited and the broker became unreachable until restarted. The listener now resets the pipe and keeps listening. A pipe handle could also be closed twice during shutdown.
- **Windows state-folder ACL was rewritten on every request.** This propagated permissions through the whole state tree on each call. It is now verified on every call and rewritten only when needed.
- **`codex` could be run from the current folder.** With the default `codex_binary = "codex"`, a file named `codex` in the MCP client's working directory (often a project checkout) was executed instead of the installed CLI. On Windows, `shutil.which` also searched the current folder first. Bare names are now looked up on `PATH` only.
- **Denials rejected without a human.** The documented denial shapes `{"permissions": {}, "scope": "turn"}` and `{"action": "decline", "content": null}` were refused with "Human review required". Any validated response that grants nothing is now accepted from the main agent.
- **Relative permission paths were resolved against the broker's working directory** instead of the task's project, so valid in-project grants were refused.
- **`service_tier: "default"`** was stored and later sent to Codex as if it were a tier ID. It now clears the tier, consistently for `codex_start` and `codex_settings`.
- **Unclear error for a failed operation key.** Retrying a `request_id` whose first attempt failed printed `inspect task None`; it now includes the original error and what to do next.
- **Test fixture race.** The fake Codex wrote its history file non-atomically, so a test could read an empty file on Windows.
- Unix broker connections rejected by the peer check no longer log unhandled-exception tracebacks. Locks are released explicitly before closing.
- The elicitation form no longer fails when a Codex question has no header.
- A missing `toolchain_paths` entry now produces a clear configuration error.

### Changed

- `stop` reports `{"stopped": true, "pid": ...}` after the broker has exited.
- A clearer error explains how to stop a broker left over from a different version or configuration.
- CLI: `--version` and help text for every command.
- CI: byte-compile step, package-install smoke test, and a source-archive check that no longer hard-codes the version.
- Documentation rewritten: README with a full installation guide, plus a smaller set of focused guides. Validation records, terminal-parity and research notes were merged into the architecture, limitations and development pages.

## 0.1.0 — 2026-11-08

Initial source release.
