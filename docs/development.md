# Development

[Back to README](../README.md) · [Contributing](../CONTRIBUTING.md)

- [Project layout](#project-layout)
- [Offline tests](#offline-tests)
- [Continuous integration](#continuous-integration)
- [Optional compatibility checks](#optional-compatibility-checks)
- [Live tests (use real Codex quota)](#live-tests-use-real-codex-quota)
- [Source archive](#source-archive)
- [Validation status](#validation-status)

## Project layout

```text
server.py                 run-from-source entry point (python3 server.py ...)
codex_local_mcp/          the package (standard library only)
tests/                    offline unittest suite and the fake Codex app-server (fake_codex.py)
scripts/                  compatibility checks, live tests, source-archive builder
docs/                     documentation
examples/                 MCP client configuration and agent-instruction examples
.github/workflows/        CI
```

The [architecture](architecture.md) page describes what each module does.

## Offline tests

```bash
python3 -m unittest discover -v
python3 -m compileall -q codex_local_mcp tests scripts server.py
```

The suite needs no Codex installation, no sign-in and no third-party packages. `tests/fake_codex.py` is a deterministic stand-in for `codex app-server` that never calls a model or runs the commands it describes. Tests copy it into private temporary folders, so missing executable bits (for example after a Windows ZIP transfer) do not matter.

The suite covers, among other things:

- the real process chain: MCP stdio client → `server.py serve` → broker → fake Codex;
- MCP handshake and version negotiation, tool validation, structured results, progress, form prompts and the no-prompt fallback;
- several clients sharing one broker, killed clients, broker crashes and recovery, and stopping the broker;
- approvals: human-only acceptance, denials without a human, restricted decision sets, subset-only permission grants, file patches outside the project, secret questions, unknown requests;
- event pagination by count and size, replay after restart, redaction, and exclusion of private reasoning;
- path safety: traversal, symlinks, replaced projects, nested projects, private folders, Unicode paths, and `codex` lookup that never uses the current folder;
- native controls: reviewer and settings changes, plan mode, service tiers, subagents and grandchildren, fork/rename/archive/delete, queues, review, compaction, revert, goals, session import, context files and images;
- Windows-specific code (named pipes, ACLs), which runs only on Windows, and macOS path aliases, which run only on macOS.

Restricted sandboxes must allow local Unix sockets and subprocesses for the IPC tests.

## Continuous integration

[`.github/workflows/tests.yml`](../.github/workflows/tests.yml) runs on every push and pull request on Ubuntu, Windows and macOS with Python 3.11 and 3.14:

1. byte-compile all sources;
2. run the offline test suite;
3. run `scripts/sdk_check.py` with the official MCP Python SDK 1.26.0;
4. `pip install .` and run the `codex-local-mcp` command;
5. build and verify the source archive.

## Optional compatibility checks

Use a separate virtual environment. These packages are **not** runtime dependencies.

```bash
python3 -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
python -m pip install "jsonschema>=4.20,<5" "mcp==1.26.0"
python scripts/check_protocol.py    # needs the installed Codex CLI; no model calls
python scripts/sdk_check.py         # uses the fake Codex; no account needed
```

`check_protocol.py` generates the app-server JSON schema from your installed Codex and validates 42 representative request and response shapes against it. Run it after upgrading Codex.

## Live tests (use real Codex quota)

These scripts use your real Codex account and spend model quota. Run them only in disposable directories. Each writes a JSON report into its state folder.

```bash
mkdir -p /tmp/cmcp-live/project && chmod 700 /tmp/cmcp-live
printf 'state_dir = "/tmp/cmcp-live/state"\n' > /tmp/cmcp-live/config.toml && chmod 600 /tmp/cmcp-live/config.toml

python3 scripts/live_smoke.py    --config /tmp/cmcp-live/config.toml --workdir /tmp/cmcp-live/project
python3 scripts/live_controls.py --config /tmp/cmcp-live/config.toml --workdir /tmp/cmcp-live/project
python3 scripts/live_extended.py         # creates its own temporary project
python3 scripts/live_child_approval.py   # creates its own temporary project
```

| Script | What it checks |
|---|---|
| `live_smoke.py` | Sign-in, models, permission profile, `ask_parent` round trip, a real coding task with unit tests, follow-up, backend restart, resume and history. Needs an **empty** project folder |
| `live_controls.py` | Mid-turn steering, interruption, and that the sandbox blocks reading a file outside the project |
| `live_extended.py` | Native subagents, *Approve for me*, settings, fork/rename, queue, goals, review, compaction, revert, archive and delete of its own fork |
| `live_child_approval.py` | A subagent's approval reaching the parent and being denied, plus context-file and image input |

The live tests never grant an approval themselves. Keep the state path short on macOS/Linux; socket paths are limited to 100 bytes. The test conversations remain in Codex's own history (`~/.codex`).

## Source archive

```bash
python3 scripts/build_source_zip.py            # writes dist/codex-mcp-VERSION-source.zip and a .sha256 file
python3 scripts/build_source_zip.py --verify   # verifies the archive for the current version
```

The archive contains the source, docs, examples, tests, scripts and CI files under one `codex-mcp/` folder, plus a `SOURCE-MANIFEST.json` with SHA-256 hashes. It excludes Git metadata, caches, virtual environments, private state and credentials. `LICENSE` is intentionally left out of this transfer archive at the owner's request, so that it does not overwrite the repository's own license file; the project is MIT-licensed. Build installable packages (`pip install .`) from a checkout that contains `LICENSE`.

## Validation status

| Date | Environment | Result |
|---|---|---|
| 2026-11-09 (v0.1.1) | Linux, Python 3.14.4, Codex CLI 0.167.0 (ChatGPT sign-in) | 83 offline tests passed (2 platform-specific skips); official MCP SDK 1.26.0 check passed; 42 protocol shapes validated; package install checked; `live_smoke.py` and `live_controls.py` passed against real Codex |
| 2026-11-08 (v0.1.0) | GitHub Actions: Ubuntu, macOS, Windows × Python 3.11, 3.14 | Ubuntu and macOS passed. Windows failed in test teardown (broker still holding `broker.log`) and in one fixture race. Both are fixed in v0.1.1 |
| 2026-11-07 (pre-release) | Linux, Codex CLI 0.167.0 | `live_extended.py` and `live_child_approval.py` passed: subagents, automatic review, queue, review, compaction, revert, fork lifecycle, image and file input |

Not verified with live tests: Claude Code's user interface, native Windows or macOS against a real Codex account, API-key sign-in, and every model in the catalog.
