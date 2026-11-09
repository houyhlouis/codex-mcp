# Security policy

Codex MCP is designed for **one trusted local user** and explicitly selected project folders. It is not a multi-user or remote service. The full trust model is in [docs/security.md](docs/security.md).

## Reporting a vulnerability

Please report vulnerabilities privately. Use GitHub's **Security → Report a vulnerability** (private vulnerability reporting) on the repository if it is enabled; otherwise contact the repository owner directly. Do not open a public issue containing exploit details.

Include:

- a minimal way to reproduce it;
- the Codex MCP version (`python3 server.py --version`), Codex version, operating system and MCP client;
- which boundary was crossed (for example project sandbox, approval routing or local IPC).

Redact credentials, personal data and project content. Never send a state folder, Codex authentication files or unredacted conversation history.

## If something goes wrong locally

1. Stop the broker: `python3 server.py stop`.
2. Keep only the evidence you need (for example the relevant part of `broker.log`) and review it before sharing.
3. If credentials may be affected, rotate them through the provider's normal process. The bridge never handles or rotates credentials.
