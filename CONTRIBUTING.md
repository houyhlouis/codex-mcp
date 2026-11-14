# Contributing

Thank you for helping improve Codex MCP. The server targets Python 3.11+ on Windows, macOS and Linux and uses only the standard library.

## Before you open a pull request

```bash
python3 -m unittest discover -v
python3 -m compileall -q codex_local_mcp tests scripts server.py
```

Both must pass. CI runs them on all three platforms. Optional checks (protocol schema, official MCP SDK) and live tests are described in [docs/development.md](docs/development.md).

## Where to make a change

[Architecture](docs/architecture.md) explains what each module does. This table tells you where a typical change goes and which tests cover it. Run the whole suite before you open a pull request; the tests named here are the ones to read first and to extend.

| If you change | Look at | Tests to read and extend |
|---|---|---|
| A tool's arguments, a result field or an error code | `tools.py`, `security.py` (`BridgeError.CODES`) | `test_mcp_contract.py`, `test_result_contract.py`. Then regenerate the [fixtures](docs/development.md#pinned-fixtures) |
| What a task does: start, send, resume, states | `service.py`, `lifecycle.py` | `test_service.py`, `test_lifecycle.py`, `test_stabilization.py` |
| Approvals, permissions, who may answer | `service.py`, `authority.py` | `test_approval_proposals.py`, `test_permission_receipts.py` |
| Settings, subagents, queue, goals, forks | `extensions.py` | `test_extensions.py`, `test_workspace_claims.py` |
| The connection to Codex and its messages | `backend.py`, `contract.py` | `test_backend_contract.py`, `test_native_wire.py`, and `scripts/check_protocol.py` |
| Stored data and upgrades | `store.py` and the module that owns the table | `test_store.py`, `test_state7.py`, `test_state8.py`. An upgrade must not read the whole event journal |
| Turn results, artifacts, cleanup, backup | `evidence.py`, `retention.py` | `test_evidence_results.py`, `test_retention.py`, `test_maintenance_cli.py` |
| Recovery reports and watching tasks | `reconciliation.py`, `outcomes.py` | `test_reconciliation_report.py`, `test_outcomes.py` |
| The MCP side: handshake, prompts, MCP 2026-07-28, Tasks | `mcp.py`, `mcp_modern.py`, `protocol_tasks.py` | `test_mcp.py`, `test_mcp_modern.py`, `test_modern_wire.py` |
| Starting and finding the broker; Windows pipes and permissions | `broker.py`, `platforms.py`, `windows.py` | `test_platforms.py`, `test_windows_pipes.py`, `test_pipe_control.py` |
| `doctor`, `incident-export` | `diagnostics.py`, `incidents.py` | `test_diagnostics.py`, `test_incidents.py` |
| Documentation | `README*.md`, `docs/`, `examples/` | `test_documentation_contract.py` checks every JSON example, tool name and internal link |

Two things make the tests trustworthy, and a change must keep both:

- **The simulated Codex cannot lie.** Every message that `tests/fake_codex.py` sends is checked against Codex's own schema. If you need the simulation to do something new, make sure the real Codex can do it too.
- **No sleeping.** Tests wait for explicit events, never for a fixed time. A test that passes only when the machine is fast will fail in CI on Windows.

A bug that only shows up in a real session can be turned into a test: ask the reporter for an [incident file](docs/troubleshooting.md#sharing-what-happened-without-sharing-your-content) and replay it, as described under [incident fixtures](docs/development.md#incident-fixtures).

## Reporting bugs and security problems

Use the issue forms. They ask for the output of `python3 server.py doctor` and remind you what must never be pasted into a public issue. Security problems go through the private route in [SECURITY.md](SECURITY.md), not through an issue.

## Ground rules

- **Keep the tool surface closed.** Do not add raw Codex RPC, an unrestricted shell, credential access, or any silent permission downgrade.
- **Never approve on someone's behalf.** Preserve the approval rules in [docs/security.md](docs/security.md). Denials are always allowed; positive approvals need a human or the explicit `approval_relay` setting.
- **Test the risky paths.** When changing recovery, retries, races or permission checks, add a regression test, ideally one that fails without your change.
- **Keep docs in sync.** Update the README and the affected page in `docs/`. The pages that have a Simplified Chinese version (README, installation, usage, configuration, troubleshooting) should stay aligned with the English text.
- **Keep examples portable.** No real credentials, personal paths, runtime databases or operator configuration.
- **Be honest about testing.** In the pull request, say what you actually ran, and on which platform. Do not claim a platform or client was tested if it was not.
- **Record user-visible changes** in [CHANGELOG.md](CHANGELOG.md). If a change requires users to restart the broker, bump the version in `pyproject.toml` and `codex_local_mcp/__init__.py`.
- **Releases follow the [release checklist](docs/development.md#release-checklist).**
