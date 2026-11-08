# Contributing

The server targets Python 3.11+ on Windows, macOS and Linux. Use the same local user for the MCP client, broker and Codex. The CI matrix runs offline tests on all three platforms; see the validation record for tests actually executed in this development session.

## Offline development checks

From the source root:

```bash
python3 -m unittest discover -v
python3 -m compileall -q codex_local_mcp tests scripts
```

The 61 offline tests use a deterministic Codex protocol fixture. They do not require a Codex login, a real model, or third-party Python packages. The environment must permit local Unix sockets/processes. Fixture executables are copied into private temporary directories so missing executable bits from source extraction do not prevent testing.

For the optional schema and official-client checks, use a separate virtual environment:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install 'jsonschema>=4.20,<5' 'mcp==1.26.0'
python scripts/check_protocol.py
python scripts/sdk_check.py
```

`check_protocol.py` requires the installed Codex CLI and generates its schemas without inference. `sdk_check.py` uses the fixture, not the real account. These packages are development dependencies only; they are not required by `server.py`.

If your Python distribution lacks `venv`/`pip`, install those components through your normal environment management process or use a prepared development environment. Do not install test dependencies into another person's environment or vendor them into this repository.

## Live checks

Live scripts consume real Codex account usage and save native test conversations. Use a disposable project, never an active production checkout:

```bash
python3 scripts/live_smoke.py --config /absolute/disposable-config.toml --workdir /absolute/disposable-project
python3 scripts/live_controls.py --config /absolute/disposable-config.toml --workdir /absolute/disposable-project
python3 scripts/live_extended.py
python3 scripts/live_child_approval.py
```

The first script requires an empty workspace. The last two create disposable directories under the system temporary directory; the extended script deletes only its own test fork. Read each script's description before running it. Use [validation.md](docs/validation.md) to distinguish previously tested behavior from new claims.

## Scope and implementation expectations

- Keep the tool allowlist explicit. Do not add raw RPC, unrestricted shell, credential extraction or silent permission downgrades.
- Preserve native approval decisions, account requirements, and known child-thread ownership boundaries.
- Test control races, recovery, duplicate delivery and permission checks when changing those paths.
- Keep `README.md` and `README.zh-CN.md` aligned on setup, capabilities and limitations; detailed reference documents can remain English.
- Keep examples portable and free of real credentials, operator configuration, runtime databases or developer-specific paths.
- Describe changes and meaningful validation in pull requests. Do not claim a platform or client UI was tested without doing so.

## Source packaging

```bash
python3 scripts/build_source_zip.py
python3 scripts/build_source_zip.py --verify dist/codex-mcp-0.1.0-source.zip
```

See [release packaging](docs/releasing.md). The package does not include `.git`, installed dependencies or private state. The project is MIT licensed. The transfer ZIP intentionally leaves out the existing repository LICENSE file.
