# Development

[Back to README](../README.md) · [Contributing](../CONTRIBUTING.md)

- [Project layout](#project-layout)
- [Offline tests](#offline-tests)
- [Continuous integration](#continuous-integration)
- [Optional compatibility checks](#optional-compatibility-checks)
- [Live tests (use real Codex quota)](#live-tests-use-real-codex-quota)
- [Release packages](#release-packages)
- [Source archive](#source-archive)
- [Release checklist](#release-checklist)
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

`python3 scripts/run_tests.py` runs the same tests with the same result, and is what CI uses. It prints each failure's traceback as soon as it happens, so a cancelled run still shows why tests failed. If one test runs longer than 180 seconds, it prints the stacks of all threads (and again every 180 seconds) without stopping the run. At the end it lists the 25 slowest tests. Arguments are passed on to test discovery, for example `python3 scripts/run_tests.py -p "test_worktrees*.py"`.

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
2. run the offline test suite with `scripts/run_tests.py`, with a limit of 45 minutes per job (Windows and macOS are much slower than Linux at starting processes);
3. run `scripts/sdk_check.py` with the official MCP Python SDK 1.26.0;
4. `pip install .` and run the `codex-local-mcp` command;
5. build a wheel and a source distribution and check them with `scripts/release_check.py`;
6. build and verify the source archive.

The workflow can also be started by hand: **Actions → Tests → Run workflow**, with the **protocol** box ticked. It then runs two extra jobs that install the Codex CLI from npm and run `scripts/check_protocol.py`: one with the tested version (0.167.0) and one with the latest version. The second job may fail without failing the workflow. It only shows whether a newer Codex has changed its protocol. Neither job signs in or runs a model.

## Optional compatibility checks

Use a separate virtual environment. These packages are **not** runtime dependencies.

```bash
python3 -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
python -m pip install "jsonschema>=4.20,<5" "mcp==1.26.0"
python scripts/check_protocol.py    # needs the installed Codex CLI; no model calls
python scripts/sdk_check.py         # uses the fake Codex; no account needed
```

`check_protocol.py` generates the app-server JSON schema from your installed Codex and checks three things against it:

- 42 hand-written sample messages;
- every message the bridge really sent to Codex in the recorded test flows;
- for the messages that Codex sends, that each field the bridge reads exists in the schema.

Its output names the Codex version and lists anything it could not check. Run it after upgrading Codex.

### Pinned fixtures

`tests/fixtures/` holds recorded messages, a small part of Codex's schema, the list of error codes and two example incidents, so that the offline tests can check them without Codex installed. Regenerate them after you change a tool's arguments, an MCP adapter, the messages sent to Codex, the error codes or the incident format. A new version number alone does not change them:

```bash
python3 scripts/check_protocol.py --codex "$(command -v codex)" --update-fixtures
```

The command needs `jsonschema` and a tested Codex version (currently 0.167.0). It prints which of the 11 files changed; running it twice on an unchanged tree reports every file as unchanged. Commit the changed fixtures together with your change.

The simulated Codex (`tests/fake_codex.py`) is checked the same way: every message it sends must be valid under Codex's own schema. A test that makes the simulation say something the real Codex could not say therefore fails.

### Incident fixtures

A file written by [`incident-export`](troubleshooting.md#sharing-what-happened-without-sharing-your-content) contains the order of events of a real task without any content. It can be replayed against the simulated Codex:

```bash
python3 scripts/replay_incident.py tests/fixtures/incident-lost-ack.json --inject drop_ack@0
```

`--inject` can be repeated. `drop@N` loses event number N, `crash@N` restarts the broker's state at that point, and `drop_ack@N` loses the acknowledgement of a request. In a test, `tests/incident_replay.py` also offers explicit gates for delays. To turn a reported problem into a regression test, add the file to `tests/fixtures/` and replay it in a test, as `tests/test_incidents.py` does for a lost acknowledgement and for a restart during a pending approval.

## Live tests (use real Codex quota)

These scripts use your real Codex account and spend model quota. Run them only in disposable directories. Each writes a JSON report into its state folder.

```bash
mkdir -p /tmp/cmcp-live/project && chmod 700 /tmp/cmcp-live
printf 'state_dir = "/tmp/cmcp-live/state"\n' > /tmp/cmcp-live/config.toml && chmod 600 /tmp/cmcp-live/config.toml

python3 scripts/live_smoke.py    --config /tmp/cmcp-live/config.toml --workdir /tmp/cmcp-live/project
python3 scripts/live_controls.py --config /tmp/cmcp-live/config.toml --workdir /tmp/cmcp-live/project
python3 scripts/live_supervision.py --config /tmp/cmcp-live2/config.toml --workdir /tmp/cmcp-live2/project
python3 scripts/live_extended.py         # creates its own temporary project
python3 scripts/live_child_approval.py   # creates its own temporary project
```

| Script | What it checks |
|---|---|
| `live_smoke.py` | Sign-in, models, permission profile, `ask_parent` round trip, a real coding task with unit tests, follow-up, backend restart, resume and history. Needs an **empty** project folder |
| `live_controls.py` | Mid-turn steering, interruption, background terminals, and that the sandbox blocks reading a file outside the project. On Linux it also checks that the leftover-process check recognises this Codex version's sandbox process, that leftovers keep the project reserved after Codex's own cleanup, and that the project is freed once they are gone. For that it stops the leftover processes of its own disposable project, and only those, through a process handle (`pidfd`) that it checks before signalling. Without `pidfd` support it stops nothing and reports the boundary check as skipped |
| `live_supervision.py` | Outcome polling, turn results and complete artifact reads (size and SHA-256), the compact and permissions views, decision receipts, a structured question, a delegation contract, and the automatic re-check after a restart. Needs an **empty** state folder and an **empty** project folder of its own, so prepare a second pair of folders in the same way |
| `live_extended.py` | Native subagents, *Approve for me*, settings, fork/rename, queue, goals, review, compaction, revert, archive and delete of its own fork |
| `live_child_approval.py` | A subagent's approval reaching the parent and being denied, plus context-file and image input |

The live tests never grant an approval themselves. `live_supervision.py` prints `PASS`, `SKIPPED` or `FAIL` for each check. `SKIPPED` means that Codex did not produce the thing to check in this run, for example no automatic review took place; `FAIL` ends the script with a non-zero exit code. Keep the state path short on macOS/Linux; socket paths are limited to 100 bytes. The test conversations remain in Codex's own history (`~/.codex`).

## Release packages

```bash
python -m pip install build "setuptools>=77.0.3" wheel
python scripts/release_check.py --require-build --require-install
```

The script builds a wheel and a source distribution and checks them:

- the version equals `codex_local_mcp.__version__` and the newest heading in `CHANGELOG.md`;
- `LICENSE` is included and the `codex-local-mcp` command is declared;
- the wheel contains every module and no tests, state files or secret-looking files;
- each package installs into a throw-away folder, and `codex-local-mcp --version` and `codex-local-mcp doctor --json` run from it.

CI runs this on every platform.

**Nothing is published.** The package is not on PyPI. `.github/workflows/publish.yml` runs only when a maintainer starts it by hand and types `publish codex-local-mcp` as confirmation, and it fails harmlessly until PyPI trusts this repository. To enable it, a maintainer would have to:

1. On PyPI, add a *pending trusted publisher* for the project `codex-local-mcp`: GitHub owner `houyhlouis`, repository `codex-mcp`, workflow `publish.yml`, environment `pypi`.
2. In the GitHub repository, create an environment named `pypi` under **Settings → Environments**. Adding required reviewers there is a good idea.
3. Run **Actions → Publish → Run workflow** on the release tag and type the confirmation.

Until that has been done and a release exists on PyPI, the documentation must keep saying that the package is installed from the downloaded folder.

## Source archive

```bash
python3 scripts/build_source_zip.py            # writes dist/codex-mcp-VERSION-source.zip and a .sha256 file
python3 scripts/build_source_zip.py --verify   # verifies the archive for the current version
```

The archive contains the source, docs, examples, tests, scripts and CI files under one `codex-mcp/` folder, plus a `SOURCE-MANIFEST.json` with SHA-256 hashes. It excludes Git metadata, caches, virtual environments, private state and credentials. `LICENSE` is intentionally left out of this transfer archive at the owner's request, so that it does not overwrite the repository's own license file; the project is MIT-licensed. Build installable packages (`pip install .`) from a checkout that contains `LICENSE`.

## Release checklist

A release is ready when every line below is true. Write down what was actually run; if a step could not be run, say so in the release notes instead of leaving it out.

**Before tagging**

1. The version is the same in `pyproject.toml`, `codex_local_mcp/__init__.py` and the newest heading of `CHANGELOG.md`, and the changelog entry has its release date.
2. CI is green for the commit to be tagged, on all six jobs (Windows, macOS, Linux × Python 3.11, 3.14). A result from an earlier commit does not count.
3. `python scripts/check_protocol.py` passes with the lowest supported Codex version and with the newest one you can install. If Codex's messages changed, the pinned fixtures were regenerated and reviewed.
4. `python scripts/release_check.py --require-build --require-install` passes.
5. An upgrade was tried: start the previous release once so that it creates its database, stop it, then run `server.py status` from the new version with the same state folder. The database must be upgraded, and a backup named `state.sqlite3.pre-v*.bak` must appear when the layout changed.
6. `live_smoke.py`, `live_controls.py` and `live_supervision.py` pass against real Codex in throw-away state folders (see [live tests](#live-tests-use-real-codex-quota)). Run the other live scripts when their area changed.
7. The README, the affected pages in `docs/` and their Chinese versions describe the new behaviour. Tool counts, version numbers and limits in the text match the code.
8. A new row in [validation status](#validation-status) records the date, the environment and the results, including what was **not** run.

**Tagging and after**

1. Tag the commit `vX.Y.Z` and create the GitHub release from the changelog entry.
2. Download **Source code (zip)** from the release, extract it into an empty folder and run `python3 server.py --version` and `python3 server.py doctor` there.
3. Tell users to run `python3 server.py stop` after updating, as the README says.

## Validation status

| Date | Environment | Result |
|---|---|---|
| 2026-11-14 (0.2.1, in development) | Linux, Python 3.11.18 and 3.14.4, Codex CLI 0.167.0 | 1221 offline tests passed (9 platform-specific skips); official MCP SDK 1.26.0 check passed; protocol check passed; package install and wheel/sdist checks passed; a small and a 274 MB state database written by v0.1.1 were upgraded with real Codex and their finished tasks needed no action; restarts with a running turn and with a running background command, and the leftover-process check, were exercised with real Codex; budgets, a plan running in two worktrees, and a worktree handoff and cleanup were exercised with real Codex; `live_smoke.py`, `live_controls.py`, `live_supervision.py`, `live_extended.py` and `live_child_approval.py` passed against real Codex. Windows and macOS have not been run for this version yet |
| 2026-11-09 (v0.1.1) | Linux, Python 3.14.4, Codex CLI 0.167.0 (ChatGPT sign-in) | 83 offline tests passed (2 platform-specific skips); official MCP SDK 1.26.0 check passed; 42 protocol shapes validated; package install checked; `live_smoke.py` and `live_controls.py` passed against real Codex |
| 2026-11-08 (v0.1.0) | GitHub Actions: Ubuntu, macOS, Windows × Python 3.11, 3.14 | Ubuntu and macOS passed. Windows failed in test teardown (broker still holding `broker.log`) and in one fixture race. Both are fixed in v0.1.1 |
| 2026-11-07 (pre-release) | Linux, Codex CLI 0.167.0 | `live_extended.py` and `live_child_approval.py` passed: subagents, automatic review, queue, review, compaction, revert, fork lifecycle, image and file input |

Not verified with live tests: Claude Code's user interface, native Windows or macOS against a real Codex account, API-key sign-in, and every model in the catalog.
