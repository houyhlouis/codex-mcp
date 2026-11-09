# Contributing

Thank you for helping improve Codex MCP. The server targets Python 3.11+ on Windows, macOS and Linux and uses only the standard library.

## Before you open a pull request

```bash
python3 -m unittest discover -v
python3 -m compileall -q codex_local_mcp tests scripts server.py
```

Both must pass. CI runs them on all three platforms. Optional checks (protocol schema, official MCP SDK) and live tests are described in [docs/development.md](docs/development.md).

## Ground rules

- **Keep the tool surface closed.** Do not add raw Codex RPC, an unrestricted shell, credential access, or any silent permission downgrade.
- **Never approve on someone's behalf.** Preserve the approval rules in [docs/security.md](docs/security.md). Denials are always allowed; positive approvals need a human or the explicit `approval_relay` setting.
- **Test the risky paths.** When changing recovery, retries, races or permission checks, add a regression test, ideally one that fails without your change.
- **Keep docs in sync.** Update the README and the affected page in `docs/`. The pages that have a Simplified Chinese version (README, installation, usage, configuration, troubleshooting) should stay aligned with the English text.
- **Keep examples portable.** No real credentials, personal paths, runtime databases or operator configuration.
- **Be honest about testing.** In the pull request, say what you actually ran, and on which platform. Do not claim a platform or client was tested if it was not.
- **Record user-visible changes** in [CHANGELOG.md](CHANGELOG.md). If a change requires users to restart the broker, bump the version in `pyproject.toml` and `codex_local_mcp/__init__.py`.
