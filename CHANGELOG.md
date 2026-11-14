# Changelog

## 0.2.1 — in development

Fixes for Windows and macOS, found by the first CI run of 0.2.0 on those systems. Includes everything in 0.2.0. Stop the broker before you update (`python3 server.py stop`), then restart your MCP client. The state database layout is the same as in 0.2.0; coming from 0.1.1, it is upgraded as described under 0.2.0.

### Fixed

- Windows with Python 3.13 or newer: private folders were refused as unsafe, so the settings, `doctor` and the maintenance commands failed. Python now gives such folders an entry for the folder's owner (*OWNER RIGHTS*), which is accepted once the owner has been checked.
- Windows: worktrees could not be used, because the path Git records for the shared Git folder (`../..`) was refused.
- Windows: `prune` could fail to show its plan for some dates.

### Changed

- Worktree commands start half as many Git processes; Git's version is checked once per Git program instead of before every command. This matters most on macOS and Windows.
- CI runs the tests with `scripts/run_tests.py`, which prints each failure's traceback at once, shows where a test hangs and lists the slowest tests. Each test job may run for 45 minutes instead of 15.
- 1221 offline tests (1208 in 0.2.0).

## 0.2.0 — 2026-11-14

Stability release that follows the audit of v0.1.1. Stop the broker before you update (`python3 server.py stop`), then restart your MCP client. The state database is upgraded automatically on the next start, after a backup is saved next to it.

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
