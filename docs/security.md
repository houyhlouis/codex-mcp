# Security model

[Back to README](../README.md)

Codex MCP is a **local, single-user** integration. It assumes the main agent, the broker and Codex all run under your own operating-system account. It protects against accidental misuse, prompt or argument injection, the wrong directory being used, and remote callers. It does **not** protect against a malicious program already running under your account. Such a program can read your files anyway.

## Local communication only

- The MCP client talks to `server.py serve` over stdin/stdout.
- `serve` talks to the broker over a **Unix socket** (macOS/Linux) or a **named pipe** (Windows). There is no TCP/HTTP listener.
- Unix sockets are mode `0600` in a `0700` folder, and the peer's user ID is checked. Windows pipes have an explicit DACL for your account and SYSTEM, reject remote clients, and check that the peer process belongs to your account and is not restricted.
- The broker accepts only the 32 named tools, a health check, a read-only health report for `doctor`, local human-review answers and shutdown. The health report contains versions and settings, never task content or credentials. There is no raw Codex protocol passthrough and no shell tool.

## Project boundary and sandbox

Each task is bound to one project directory chosen by the main agent. Choosing a directory grants Codex access to it, so the main agent should choose deliberately. The server refuses:

- relative paths, `..`, symbolic links, Windows junctions/reparse points, UNC and device paths;
- your home folder itself and filesystem roots;
- the server's state folder, `~/.ssh` and `~/.aws`, anything inside them, and any folder that contains them;
- any folder that contains Codex's `auth.json` or the server's settings file.

Every Codex conversation uses a **named Codex permission profile** created for this server:

- the project is the only workspace root, with `read` or `write` access;
- minimal system runtime paths, the Codex executable and any configured `toolchain_paths` are readable;
- the server's state folder and settings file are explicitly denied;
- network access for commands is off unless an approval allows it.

Before sending any work, the server checks that Codex confirmed this profile, the working directory, the workspace root and the `on-request` approval policy. If anything differs, it refuses to start. It never falls back to full access or disables the sandbox.

Independent tasks cannot work in overlapping directories at the same time. Codex's own subagents are adopted only after the server checks that their parent chain leads to an owned task and their directory is inside the parent's project.

The sandbox limits Codex's tool commands, not the Codex process itself. Codex still needs its credentials and network access to reach the model service. External MCP servers and plugins configured in Codex are **disabled** for bridge tasks unless you set `allow_external_tools = true`. They do not obey the project sandbox.

On Linux the server reads the process list (`/proc`) of your own user account to find Codex sandbox processes that are still running in a project. It reads only the name of the process, the two folder arguments of Codex's sandbox and the start time. It does not store command lines or environments, and it never signals a process. Set `sandbox_process_check = false` to turn this off.

### Worktrees

`codex_worktrees` is off unless you set `worktrees = true`. When it is on, the server itself runs Git outside Codex's sandbox to create, inspect, hand off and remove worktrees. It therefore treats the repository as data that a task may have changed:

- Git runs without a shell, with your system and personal Git configuration ignored, hooks and the file-system monitor disabled, and the worktree's Git directory named explicitly instead of read from the `.git` file in the worktree.
- Repositories whose configuration could make Git run a program or read data from elsewhere are refused: filters, included or conditional configuration, program settings, object alternates, sparse checkouts and partial clones. The configuration is checked again right before each Git call.
- While any task works in the main checkout (which can change the repository's configuration), the server does not run Git on its worktrees.
- Folders are created only where you expect them, never through a symbolic link, and a worktree is removed only when it holds nothing that could be unsaved work. The server never merges, pushes or forces.

Tasks in a worktree get no access to the repository's shared Git folder, so they cannot change another worktree's branches or the repository's configuration.

### Budgets

Budgets only limit what the server admits and stop work cooperatively. They do not change your Codex plan, buy anything or show costs, and a stop does not undo commands that already ran.

## Approvals

Codex enforces its policies; the server only routes the requests. A *positive* answer to an approval, a user question or an external form requires one of these:

1. a human answering the client's form prompt (`codex_review_request`), or
2. a human using the terminal command `server.py review REQUEST_ID`, or
3. the operator setting `approval_relay = true`, which delegates this trust to the main agent.

The main agent can always deny, and can always answer `ask_parent` questions, which provide context rather than permission. *Approve for me* uses Codex's own automatic reviewer and never fabricates an approval.

Further restrictions:

- Only one-shot approvals are supported. There are no session-wide approvals, rule changes or persistent network rules.
- A file approval is accepted only if every path it affects is inside the project: the changed file, the destination of a rename, and any requested root. Symbolic links are resolved before the check. A patch in an unknown shape cannot be accepted.
- A file approval is bound to the exact patch that was shown, through a hash. If Codex changes the patch before the answer is sent, the answer is rejected and the request stays open for a new review. The check is repeated at the moment the answer is written to Codex.
- A file change in which the server hid a value that looked like a credential can be accepted by a person only, in the client's prompt or the terminal review, which both show the warning. `approval_relay` does not cover this case. The reason is simple: the reviewer cannot see what is hidden, and only the person can know whether that is acceptable.
- Every answer leaves a decision receipt that says through which channel it came (client prompt, terminal, relay, the main agent's denial, or Codex's own reviewer). The channel is set by the server's own adapter, not by anything the main agent can write. A receipt holds no answer text and no file content, and it is not a permission for later requests.
- An answer that grants nothing counts as a denial, whatever its exact form.
- Permission grants must be a subset of the request, apply to the current turn only, and stay inside the project.
- Requests expire when the turn ends or Codex restarts. Dismissing a prompt, a timeout or a missing UI never approves anything.
- Questions marked secret are refused. Use Codex's own sign-in for credentials.
- On MCP 2026-07-28 the same rules apply. A person's answer is accepted only as the client's reply to a form that the server issued for that exact request, once, within 5 minutes, and only if the client declared that it can show forms. Tool arguments can never carry such an answer. Cancelling an MCP task interrupts the turn; it approves nothing.

## Credentials

The server never opens Codex's authentication files, exports tokens, or logs in or out. `codex_status` reports only whether you are signed in and the sign-in type. Do not put passwords, tokens or keys in prompts or answers.

## Stored data

The state folder holds a SQLite database with task records and an event journal (prompts, Codex messages, command output and diffs), copies of images you attached, and `broker.log`. When a turn finishes, the content of the files it changed can also be saved there, so that a [turn result](tools.md#turn-results-and-artifacts) can show it later.

- Common secret patterns (API keys, bearer tokens, JWTs, `password=` and similar) and sensitive field names are redacted before storage and before results are returned. Pattern-based redaction cannot catch every secret.
- Codex's raw private reasoning is never stored.
- Answers to questions are not journaled by the server, although Codex may include them in its own history.
- The database has a schema version. Before an update changes its layout, the server saves a private backup next to it (`state.sqlite3.pre-v*.bak`; the newest three are kept). A database written by a newer version is refused and left unchanged.
- File content is read only for files that Codex reported changing, inside the task's project, and it is redacted like everything else before it is returned. A reference to a changed file is refused when the file no longer matches what the turn produced. The hash of the unredacted file stays inside the database and is never shown.
- Nothing is encrypted. Apart from old upgrade backups, nothing is deleted automatically. Treat the state folder, its backups and Codex's own history in `~/.codex` as sensitive.
- `server.py incident-export` writes the structure of a task for a bug report. It leaves out all content (prompts, output, paths, commands) rather than trying to find secrets in it, shows a preview first, and writes only what you confirmed.
- You decide what is kept. `server.py backup` makes a checked copy, also while the broker is running. `server.py prune` removes old data, but only after it has shown a plan, and never data that a running task, an open approval or an unresolved operation still needs. The main agent cannot run either command. See [backing up and cleaning up](installation.md#backing-up-and-cleaning-up). Do not copy or delete the database files by hand while the broker is running.

## Reporting vulnerabilities

See [SECURITY.md](../SECURITY.md).
