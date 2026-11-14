# Troubleshooting

English | [简体中文](troubleshooting.zh-CN.md) · [Back to README](../README.md)

Run the commands from the `codex-mcp` folder. On Windows, use `py -3` instead of `python3`. If you use a settings file, add `--config /path/config.toml` before the command.

## First checks

```bash
python3 --version              # 3.11 or newer
codex --version                # 0.167.0 or newer
codex login status             # signed in
python3 server.py --version
python3 server.py status       # starts the broker; should show "signed_in": true
```

`codex doctor` checks the Codex installation itself. The broker's private log is `broker.log` in the state folder: `~/.local/state/codex-mcp/` on macOS/Linux, `%LOCALAPPDATA%\CodexMCP\` on Windows.

## Run the doctor first

```bash
python3 server.py doctor
```

`doctor` checks the installation. Its report starts with a one-line verdict, then lists problems and warnings, each with what to do about it, then the checks that passed, and finally the paths and settings it found. It does not use a model. By default it starts nothing and changes nothing: it only creates a private temporary folder and removes it again.

It reports:

- the versions of the server, Python and Codex, and whether your Codex version is a tested one;
- which Codex program is used and how it was found;
- the settings file and the state folder, and whether the state folder is private;
- whether local sockets (named pipes on Windows) work in this environment;
- whether a broker is running, and whether it was started by a different version or with different settings;
- the state database: its version, its size, a quick integrity check, whether its tables match its version, and the backups found next to it.

| Option | What it adds |
|---|---|
| `--json` | Machine-readable output |
| `--start-broker` | Lets `doctor` start the broker and Codex, so that it can also report whether Codex is signed in. This creates the state folder |
| `--schema` | Generates Codex's protocol description and checks the server's messages against it. No model is used |

The exit code is 0 when everything is fine, 1 when there are warnings and 2 when there are errors. If you use a settings file, pass it as in your MCP client configuration: `python3 server.py --config /path/config.toml doctor`.

Run `doctor` in the same environment as your MCP client. A check that passes in your terminal can fail inside a restricted container, and the other way round.

## Common problems

| Symptom | Cause and fix |
|---|---|
| `Codex executable not found` | Codex is not on the `PATH` that the MCP client sees. Set `codex_binary` to the absolute path in a [settings file](configuration.md). If the message ends with `Empty or relative PATH entries were ignored`, your `PATH` contains entries such as `.` that are never searched. |
| `codex_binary=... is a relative path` | Write the absolute path of the Codex program in `codex_binary`. See [How Codex is found](configuration.md#how-codex-is-found). |
| `Codex 0.167.0 or newer is required` | Run `codex update`, then `python3 server.py stop`. |
| `optional_native_capability_unavailable: ...` | The installed Codex lacks an optional feature that this tool needs. Use another control, or install a tested Codex version and run `python3 server.py stop`. |
| `Codex event ingress budget exceeded` | Codex produced events faster than they could be stored, so the connection to Codex was reset to stay within memory limits. Call `codex_resume`, then read `codex_history`. |
| `Task has a native protocol error` | Codex sent a message this version does not understand. Call `codex_resume` and check `codex_history`. If it happens again, your Codex is probably newer than the tested version: look at `compatibility` in the output of `server.py status`. |
| `Native thread scope is unverified; resume before ...` | The conversation has not been checked against Codex since the last restart or reconnect. Call `codex_resume` for the task, then repeat the action. A pending approval stays pending in the meantime, and you can still deny it. |
| `Codex connection changed ...; resume before retrying` | Codex restarted while the action was on its way. Call `codex_resume`, check `codex_get`, then decide whether to repeat it. |
| `Drive-relative approval paths are not supported` | A patch or permission request used a Windows path such as `C:folder\file`. Deny it; Codex should use a path inside the project. |
| `codex` is missing or failed in `/mcp` / the client's server list | Run the exact command from your client configuration in a terminal (with `status` instead of `serve`) and read the error. Check that the path to `server.py` is absolute. |
| `A broker started by a different version or configuration is still running` | You updated the code or changed settings. Run `python3 server.py stop` (with the same `--config`, if any) and retry. |
| `Broker failed to start` / `did not become ready` | Read `broker.log` in the state folder. Some locked-down containers forbid Unix sockets. |
| `Work directory ... missing` / `must be an absolute path` | Pass an absolute path, or let the main agent create the folder with `codex_projects` (`action: "create"`). |
| `Select a dedicated project directory, not the home directory` | Choose a project subfolder. Your whole home folder or a drive root cannot be a project. |
| `Project directory overlaps private state` | The project contains, or is inside, the state folder or the settings file. Use another folder. |
| `State database schema version ... is newer than supported version ...` | The state folder was last used by a newer codex-mcp. Update the code to that version, or point `state_dir` to another folder. The database was not changed. |
| `State database migration failed; previous state was preserved` | The upgrade was undone. Keep the state folder and the `state.sqlite3.pre-v*.bak` backup in it, and report the problem. |
| `Another task is active in this project` | Another task still reserves the folder. The message names the task and the reason after `Blocking workspace claims:`, for example `[goal=active]` or `[background=active]`. Pause or clear the goal, empty the queue or stop the background terminals, then call `codex_resume` for that task. A project is freed automatically once Codex confirms nothing can still run in it; see [Why a project stays busy](tools.md#why-a-project-stays-busy). |
| `Commands started before the restart may still be running in this project` | Work was running when the broker stopped or crashed, and Codex can no longer see those commands. Follow [Commands that keep running](#commands-that-keep-running), then call `codex_resume` for the task that is named. |
| `N Codex sandbox processes are still running in this project (PID ...)` | Linux only. Commands that Codex no longer lists are still running in the folder. Stop them with `kill PID`; the project is then freed without another call. If the message ends with "No task accounts for them", they were started outside this server, for example with the Codex CLI; wait until they end or stop them, then retry with a new `request_id`. See [Leftover processes on Linux](tools.md#leftover-processes-on-linux). |
| `This conversation is archived. For archived task ...` | An archived task still reserves the project. Do what the message says: unarchive it with `codex_archive` and call `codex_resume`, or remove it with `codex_delete`. |
| `Task-family reconciliation did not settle within 5 seconds` | `codex_delete` waited for the server's own checks of that task family, for example right after a start, and they did not finish in time. Nothing was deleted. Look at `codex_get`, then retry with a new `request_id`. |
| `send and review nodes must name an earlier start node as target` | In a [plan](tools.md#plans), a `send` or `review` step uses the conversation of an earlier `start` step: write `"target": {"node": "ID"}` and list that step in `after`. |
| `A plan stores its prompts until they run; remove the value that looks like a password, token or key ...` | Take the secret out of the named step's prompt. Plans keep their prompts in the state database until the step runs. |
| `This plan was removed by cleanup and cannot run again under the same identity` | Change the plan's `name` and submit it again; it then runs as a new plan. |
| A plan step stays `uncertain` | Its outcome is unknown, for example after a restart. Call `codex_resume` for the task that `inspect` names; the plan continues afterwards. If the step still stays `uncertain`, the server could not tell which turn was the step's; see [Ending a plan that stays blocked](tools.md#plans). |
| `Stored dependency plan operation identity is inconsistent; inspect state before running more work` | The stored record of a plan step does not match the step. Nothing more is started for it. Run `doctor`, read the plan with `inspect` and the task with `codex_history`, then cancel the plan. |
| `This task never reached Codex; pass confirm_thread_id equal to the task id to delete it` | The task has no Codex conversation. Pass the task ID as `confirm_thread_id`. See [A task that never reached Codex](tools.md#a-task-that-never-reached-codex). |
| `No Codex thread was created for this task` | The task's start is still in progress, or its records do not prove that nothing ran. Wait until the start has finished, check the task with `codex_get` and call `codex_resume` again. If the message stays, run `doctor` and [report it](#reporting-a-problem) with an incident export. |
| `Native turn identity is incomplete; workspace ownership was retained` and other messages ending in `workspace ownership was retained` | After a restart, the server could not prove from Codex's history or from its own records that the task's last turn ended, so the project stays reserved. Read `codex_history`, then call `codex_resume` again. |
| `Inline review turn identity is unresolved; use codex_resume, then codex_history` or `Inline review turn identity is ambiguous across threads; ...` | The server cannot prove which turn is an inline review, usually a review inside a subagent. The task stays busy so that nothing is guessed. Call `codex_resume`, then read `codex_history`. See "Turns inside a turn" under `codex_history` in [Tools](tools.md). |
| `Revert requires a root turn id; use logical_turn_id from codex_history` | The ID belongs to a turn inside another turn, such as an inline review. Use that entry's `logical_turn_id`. |
| `Turn was not found in bounded native history; inspect codex_history before reverting` | The server looked through the most recent 500 turns and did not find this one. Check the ID in `codex_history`. |
| `Family budget ... reached: observed ..., limit ...` | The task family's [budget](tools.md#family-usage-and-budgets) is used up, or a soft limit stopped it. Raise or remove the limit with `codex_settings` on the root task (`scope: "next"`, `budget`), then retry with a new `request_id`. |
| `Cooperative budget stop; may overshoot` | A soft budget limit was crossed and the running turn was interrupted. Read `budget_stop` and `family_usage` in `codex_get`. Nothing was undone. |
| `expected_turn_id requires an active matching turn` | `codex_delegate` was given a turn that has already ended. Leave out `expected_turn_id` when the parent is idle. |
| `Managed worktree actions require worktrees = true` | Set `worktrees = true` in the settings file and restart the broker. |
| `Worktree changes need the main checkout to be idle` | A task works in the repository's main checkout. Wait until it has finished, then retry with a new `request_id`. See [Worktrees](tools.md#worktrees). |
| `... prepare this worktree manually instead` | The repository uses a Git feature that could make the server's Git run a program or read data from elsewhere (filters such as Git LFS, included configuration, sparse checkout and others). Create the worktree yourself with `git worktree add` and select it as a project. |
| `The worktree has unsaved or ignored files ...; nothing was removed` | Save or move the changes in the worktree first. Use `handoff` to keep them as a patch. |
| `The worktree has commits outside its base; use keep_branch=True ...` | Cleanup with `keep_branch: true` removes the folder and keeps the branch with its commits. |
| `The process list could not be read; workspace ownership is kept` | Linux only. The server could not read `/proc` and therefore cannot tell whether a leftover process is still running. Run `python3 server.py doctor`, which says why. If `/proc` cannot be made readable, set `sandbox_process_check = false` and restart the broker. |
| `Workspace changed during the process check` | The project's reservations changed three times while the server was looking for leftover processes. Look at `codex_get`, then retry with a new `request_id`. |
| `This conversation no longer exists in Codex or cannot be read` | The conversation was removed from Codex, for example with its session files. Free the project with `codex_delete` and the `confirm_thread_id` from the message. The task's audit records are kept. See [A task whose conversation is gone](tools.md#a-task-whose-conversation-is-gone). |
| `File approval proposal is stale or unknown` | Codex changed the patch after it was shown. Review the request again. |
| `Another task is active in this project` | Only one independent task may work in a folder at a time. Wait, interrupt it, or use a separate folder or worktree. |
| `Concurrent task limit reached` | Finish or interrupt a task, or raise `max_tasks`. |
| `Configuration must be owned by this user...` | Run `chmod 600` on the settings file (macOS/Linux). On Windows, make sure only your account can modify it. |
| `State directory must be owned by this user with mode 0700` | `chmod 700` the state folder, or choose a new `state_dir`. |
| `state_dir is too long for a Unix socket path` | Choose a shorter `state_dir`. |
| `The legacy unelevated Windows sandbox cannot enforce denied reads` | Switch Codex to its default Windows sandbox (see [installation](installation.md#windows-notes)). |
| A tool inside Codex is "not found" (for example a compiler in an unusual location) | Add its folder to `toolchain_paths`, then `stop` the broker. |
| `Model is not in the visible Codex catalog` / `Unsupported reasoning effort` | Call `codex_models` and use a listed combination. |
| `Previous operation with this request_id is uncertain` | That key was already used and failed or was interrupted. Inspect the task, then use a new `request_id`. |
| `Resume could not commit a consistent native recovery snapshot` | The task changed while `codex_resume` was reading its state from Codex, three times in a row. Nothing was acknowledged or released. Look at `codex_get`, then call `codex_resume` again with a new `request_id`. |
| The same refusal comes back although the cause is gone | A call that was refused before anything reached Codex is saved with its `request_id`, and the same key returns the same error. Send the call again with a new `request_id`. |
| `request_id was already used with different arguments` | Use a new, unique `request_id` for each new action. |
| `Operation key has expired` | The saved result of that call was removed by `prune`. The key stays blocked on purpose. Inspect the task and use a new `request_id` for new work. |
| `Event cursor has expired` | The events at your cursor were removed by `prune`. Continue from `resume_after` in the error. |
| `Pruning requires a stopped broker` | Run `python3 server.py stop` (with the same `--config`), then `prune` again. |
| The state folder has become large | See [backing up and cleaning up](installation.md#backing-up-and-cleaning-up). Nothing is removed automatically. |

## A task is waiting

A `waiting` task needs an answer.

1. List pending requests with `codex_requests` (or read `pending_requests` from `codex_get`).
2. For a `parent_question`, the main agent answers with `codex_respond` and `{"text": "..."}`.
3. For approvals and user questions, use `codex_review_request`. If your client cannot show a prompt, run `python3 server.py review REQUEST_ID` in a terminal.
4. Requests expire when Codex restarts. If an answer is rejected as expired, call `codex_resume` and let Codex ask again.

If cancelling a subagent's approval leaves its parent waiting, interrupt the parent's turn, wait for `interrupted`, and send a follow-up that explains what to do instead.

## Tasks stop when the client closes

Some MCP hosts kill all child processes on exit, which also kills the broker. Start the broker from a normal terminal with `python3 server.py start` before opening the client.

## Recovering after a crash or restart

- Active tasks become `needs_resume`. Call `codex_resume`, read `codex_history`, then send a follow-up. Nothing is replayed automatically.
- Keep reading `codex_events` from your last saved cursor; committed events are never lost.
- An `uncertain` status means an action's acknowledgement was lost. Check `codex_get` and `codex_history` before repeating anything.
- If a queued message "is no longer pending", Codex has probably already started it. Check the task instead of adding it again.
- A task that had finished before the restart needs nothing; its project is free again within seconds. A task that was working keeps its project reserved until you call `codex_resume` for it. Read the next section first.

## Commands that keep running

The server knows about running commands only what Codex tells it. Three situations can leave a command running in a project although no task shows it:

- The broker was stopped, or crashed, while a turn or a background terminal was running. Stopping the broker does not stop those commands.
- A command detached itself from Codex, for example with `nohup ... &`.
- On Linux with Codex 0.167.0, a background terminal can keep running after Codex has reported it as terminated. This was measured with `codex_terminals` `terminate` and `clean`; it is a problem in Codex, not in this server.

**On Linux the server looks for such leftovers itself.** It keeps the project reserved while it finds any, names their PIDs in the refusal and in `codex_get`, and frees the project once they are gone. `python3 server.py doctor` lists them too. On macOS and Windows, and for a process that has left Codex's sandbox, you have to look yourself.

To look for leftovers and stop them:

```bash
pgrep -af codex-linux-sandbox        # Linux: sandboxed commands started by Codex, with their project folder
ps -eo pid,etime,args | grep /path/to/project
kill PID
```

On macOS use `ps` in the same way. On Windows use Task Manager or `Get-Process`. On Linux, the process to stop is the `codex-linux-sandbox` one whose command line names your project; the commands inside it end with it. Then call `codex_resume` for the task, if the server asked for it with the restart warning. For leftovers found by the Linux check no call is needed.

To avoid the situation, check `codex_list` for running tasks and `codex_terminals` for background terminals before you run `server.py stop`.

## Reporting a problem

Include the output of `python3 server.py --version`, `codex --version`, your operating system, the tool call that failed and the (redacted) error message. Say whether the offline tests (`python3 -m unittest discover`) pass. Do not upload the state folder, `~/.codex` or full conversation history; they can contain private code and prompts. For security issues, see [SECURITY.md](../SECURITY.md).

### Sharing what happened without sharing your content

If the problem is about how a task behaved (a task that got stuck, an answer that was lost, a wrong state), you can attach the *structure* of the task without its content:

```bash
python3 server.py incident-export --task-id TASK_ID
```

This prints a preview and writes nothing. The preview has the sequence of events and requests of the task. Every text, path, command and output is replaced by a placeholder such as `{"$omitted": {"type": "string", "length": 412}}`, and every ID by a neutral reference such as `ref-0003`. Read it. If you are happy to share it, repeat the command with the `preview_id` that it printed:

```bash
python3 server.py incident-export --task-id TASK_ID --output incident.json --confirm-preview PREVIEW_ID
```

The file is written only if the data is still the same as in the preview. The command does not start the broker or Codex, sends nothing anywhere and never overwrites a file. It also does not upgrade the database: right after an update, start the server once (`python3 server.py status`) before you export.

| Option | Meaning |
|---|---|
| `--task-id` | The task. Can be given up to 8 times |
| `--after`, `--limit` | Which events to include: those after a cursor (default 0), 200 by default and 1,000 at most |
| `--json` | Print JSON |
| `--output`, `--confirm-preview` | Write the file, after you have seen the preview |

A maintainer can replay such a file against the simulated Codex and turn it into a test; see [development](development.md#incident-fixtures).
