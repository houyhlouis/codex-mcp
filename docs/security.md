# Security model

[Back to README](../README.md)

Codex MCP is a **local, single-user** integration. It assumes the main agent, the broker and Codex all run under your own operating-system account. It protects against accidental misuse, prompt or argument injection, the wrong directory being used, and remote callers. It does **not** protect against a malicious program already running under your account. Such a program can read your files anyway.

## Local communication only

- The MCP client talks to `server.py serve` over stdin/stdout.
- `serve` talks to the broker over a **Unix socket** (macOS/Linux) or a **named pipe** (Windows). There is no TCP/HTTP listener.
- Unix sockets are mode `0600` in a `0700` folder, and the peer's user ID is checked. Windows pipes have an explicit DACL for your account and SYSTEM, reject remote clients, and check that the peer process belongs to your account and is not restricted.
- The broker accepts only the 32 named tools, a health check, local human-review answers and shutdown. There is no raw Codex protocol passthrough and no shell tool.

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

## Approvals

Codex enforces its policies; the server only routes the requests. A *positive* answer to an approval, a user question or an external form requires one of these:

1. a human answering the client's form prompt (`codex_review_request`), or
2. a human using the terminal command `server.py review REQUEST_ID`, or
3. the operator setting `approval_relay = true`, which delegates this trust to the main agent.

The main agent can always deny, and can always answer `ask_parent` questions, which provide context rather than permission. *Approve for me* uses Codex's own automatic reviewer and never fabricates an approval.

Further restrictions:

- Only one-shot approvals are supported. There are no session-wide approvals, rule changes or persistent network rules.
- File approvals that would touch paths outside the project are refused.
- Permission grants must be a subset of the request, apply to the current turn only, and stay inside the project.
- Requests expire when the turn ends or Codex restarts. Dismissing a prompt, a timeout or a missing UI never approves anything.
- Questions marked secret are refused. Use Codex's own sign-in for credentials.

## Credentials

The server never opens Codex's authentication files, exports tokens, or logs in or out. `codex_status` reports only whether you are signed in and the sign-in type. Do not put passwords, tokens or keys in prompts or answers.

## Stored data

The state folder holds a SQLite database with task records and an event journal (prompts, Codex messages, command output and diffs), copies of images you attached, and `broker.log`.

- Common secret patterns (API keys, bearer tokens, JWTs, `password=` and similar) and sensitive field names are redacted before storage and before results are returned. Pattern-based redaction cannot catch every secret.
- Codex's raw private reasoning is never stored.
- Answers to questions are not journaled by the server, although Codex may include them in its own history.
- Nothing is encrypted or deleted automatically. Treat the state folder (and Codex's own history in `~/.codex`) as sensitive. Stop the broker before backing up or deleting it.

## Reporting vulnerabilities

See [SECURITY.md](../SECURITY.md).
