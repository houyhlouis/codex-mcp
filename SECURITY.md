# Security policy

Codex MCP is designed for **one trusted local user** and explicitly selected project folders. It is not a multi-user or remote service. The full trust model is in [docs/security.md](docs/security.md).

## Supported versions

Security fixes are made on the latest release only. Please check that the problem still exists there before reporting it.

## Reporting a vulnerability

Report vulnerabilities privately through GitHub:

1. Open the [new security advisory form](https://github.com/houyhlouis/codex-mcp/security/advisories/new). You can also reach it from the repository: **Security → Report a vulnerability**.
2. Describe the problem. Only the maintainer can read the report.

You need a GitHub account. If the form is not available to you, open a normal issue titled **"Security contact request"** and write nothing else in it. The maintainer will reply with a private way to continue. Never put the details of a vulnerability in a public issue, discussion or pull request.

### What to include

- A minimal way to reproduce the problem.
- The Codex MCP version (`python3 server.py --version`), the Codex version, your operating system and your MCP client.
- Which boundary was crossed, for example the project sandbox, approval routing or the local socket.
- The output of `python3 server.py doctor`, if it is relevant.

### What to leave out

- Passwords, tokens, API keys and Codex's `auth.json`.
- The state folder or the `state.sqlite3` database. They contain your prompts and Codex's output.
- Complete logs or conversation history. Copy only the lines that matter, and read them before you send them.

Replace personal folder names and project content with placeholders.

### What happens next

You get an acknowledgement in the advisory. This is a small project maintained in spare time, so there is no guaranteed response time. When a fix is ready, it is released together with a changelog entry, and the advisory is published. You are credited unless you ask not to be.

## What counts as a vulnerability

These are in scope:

- Codex's commands reading or changing files outside the selected project in a way the [sandbox rules](docs/security.md#project-boundary-and-sandbox) do not allow.
- An approval that is accepted without a person, without Codex's own reviewer, and without the `approval_relay` setting.
- Another user account on the same computer reading the state folder or connecting to the broker.
- A secret that should have been redacted appearing in a tool result or the log.

These are not vulnerabilities in this project:

- Anything that needs the attacker to already run programs as your own user account. The server cannot protect you from yourself.
- What Codex or the model decides to do inside the project folder with the permissions you gave it.
- Problems in the Codex CLI itself. Report those to OpenAI.

## If something goes wrong locally

1. Stop the broker: `python3 server.py stop`.
2. Keep only the evidence you need, for example the relevant part of `broker.log`, and review it before sharing.
3. If credentials may be affected, rotate them through the provider's normal process. The server never handles or rotates credentials.
