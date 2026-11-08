# Validation record

English | [简体中文](validation.zh-CN.md)

Implementation environment: Linux, Python 3.14.4, installed **Codex CLI 0.167.0**, native ChatGPT sign-in. Checks ran November 7, 2026 (America/Los_Angeles). No credentials were opened/copied, no remote listener was enabled, and no permissive sandbox fallback was used.

## Automated tests

```bash
# From the source root:
python3 -m unittest discover -v
```

The suite contains **61 tests** using `unittest` and a deterministic Codex protocol fixture. The fixture is a separate executable process and never calls a model or executes the represented coding commands. The suite covers:

- Actual MCP stdio framing, initialization, discovery, tool validation, structured results and version negotiation.
- The complete process path: MCP adapter → private Unix broker → Codex protocol fixture.
- Multiple clients and projects; prevention of overlapping active projects.
- Client termination/reconnection while a task continues.
- Abrupt broker death, recovery, persisted history and stale-approval rejection.
- Completed, failed, interrupted and uncertain operations; no replay after a lost acknowledgement.
- Dynamic model/effort validation; persistent follow-ups and model changes.
- Parent questions, native questions, nonblocking questions, human approval, denial, restricted decision choices and operator-enabled relay.
- MCP form elicitation, dismissal without approval, and the fallback for clients lacking elicitation.
- File-patch review context, denial of outside-project file grants, subset/turn-only permission grants, rejection of secret answers, and unknown-request denial.
- Durable event pagination, byte-budget pagination without dropped records, filtering of raw reasoning, and auth/token redaction.
- Path traversal, absolute paths, symlinks, replaced projects, changed workspace scopes, private socket modes and unexposed raw APIs.
- Poll cancellation without interrupting work, optional progress notifications and harmless late UI responses.
- Native reviewer selection and settings changes; model-tier validation; plan mode; child/grandchild ownership, child approval routing and resubscription after restart.
- Native fork/rename/archive/restore/delete, refusal of active or outside-project cascades, queue mutation/execution, review, compaction and history rollback.
- Explicit goal intent without implicit budgets, scoped native-session import, skill/catalog selection, file/image ingestion and immutable image snapshots.

The execution environment's outer restrictions prohibit creating Unix sockets without the normal local-execution permission available through this session. The first IPC test run failed for that reason; rerunning with permission to create private local sockets passed. The application itself never disables its own or Codex's controls.

## Installed protocol compatibility

```bash
python3 scripts/check_protocol.py
```

This optional development check requires `jsonschema`. It generates the experimental schemas from the installed CLI and validates **42 representative request/response shapes**, including the expanded native settings, lifecycle, queue, review, goal and terminal controls. All 42 passed against 0.167.0. It does not call a model.

## Official MCP client interoperability

```bash
# In a separate test environment with mcp==1.26.0 installed:
python scripts/sdk_check.py
```

Passed with the official MCP Python **1.26.0** client: negotiated 2025-11-25, validated tool descriptors, consumed structured results, completed a simulated coding lifecycle and handled a form elicitation round trip. The current interface has 32 tools. The SDK was installed only into a temporary test environment; it is not a runtime dependency of this server.

## Live installed-Codex tests

These are opt-in, consume real account usage, and must use a **disposable** configured project. `live_smoke.py` requires the workspace to be empty. Each script writes a private JSON result report into its configured state directory.

```bash
python3 scripts/live_smoke.py --config /absolute/disposable-config.toml --workdir /absolute/disposable-project
python3 scripts/live_controls.py --config /absolute/disposable-config.toml --workdir /absolute/disposable-project
```

| Live check | Observed result |
|---|---|
| App-server initialization and native account access | Passed; only auth type/status exposed |
| Model/effort discovery | Passed with the installed account's catalog |
| Named read/write profile and exact project-root acknowledgement | Passed |
| Codex asks `ask_parent` and receives a main-agent reply | Passed |
| Coding task | Created `calculator.py` implementing `add(a, b)` and `test_calculator.py` |
| Actual tests in that project | `python3 -m unittest -v`, **3 tests passed**, exit code 0 |
| Persistent follow-up | Same native thread; described the function/test cases from the prior conversation |
| Backend close/restart and resume | Passed; persisted history returned both turns |
| Mid-turn steering | Accepted while a controlled sleep command was running |
| Interruption | Native `turn/completed` confirmed `interrupted` |
| Outside-project read | A sandboxed `cat` of a synthetic marker outside the approved project failed; its contents did not enter events |

The initial restrictive profile omitted the standalone Codex executable under the home directory, causing bubblewrap to report that executable missing. The integration was corrected to grant read access to that **exact binary**, and the live coding test then passed. It did not grant general home-directory access or approve an unsandboxed retry. One initial approval also demonstrated that native `availableDecisions` can omit `decline`; the test now chooses `cancel` when that is the offered denial option.

The live coding run also observed `rg` unavailable inside the minimal runtime profile. Codex used standard directory tools, then completed the implementation and all three Python tests. This is recorded as a toolchain-access limitation, not hidden as a successful command. Specific custom toolchain paths can be configured read-only.

## Not claimed as live-tested

- Claude Code's actual UI: not installed here. Setup syntax was verified against its current official documentation; protocol compatibility was tested independently.
- API-key authentication, all catalog models/efforts, externally configured MCP services, connector URL authorization and every external form schema.
- Positive human-UI approval of a live escalation, persistent grants or global policy modification. Native automatic review of a harmless print escalation was tested separately as described below.
- Native Windows/macOS runtime execution, future Codex versions or clients requiring only MCP 2026. Native adapters have since been implemented; their execution was not available on this Linux development host.
- Exact preservation of transient stream deltas across a sudden broker/backend failure; only committed journal replay and persisted-history recovery are promised.

See [limitations](limitations.md) for the investigated alternatives and [security](security.md) for the trust model.

## Earlier live checks for extended controls

```bash
# Each creates its own disposable /tmp project/config/state and consumes usage:
python3 scripts/live_extended.py
python3 scripts/live_child_approval.py
```

The extended live test passed native child discovery/subscription, parent event aggregation, explicit `auto_review` selection and native automatic-review results. Codex's reviewer approved the harmless `printf AUTO_REVIEW_OK` escalation; its sandbox and reviewer controls remained active. The bridge did not send a synthetic approval.

The same test passed native plan/reviewer changes, fork/rename, queue add/list/consumption, paused goal set/get/clear, capabilities/skills/background-terminal/rate-limit reads, code review, compaction, history revert and archive/restore/deletion of its own disposable fork. No existing user conversation or project was deleted. Queue update/delete/reorder and process termination are covered by fixture tests; the live idle queue consumed an entry before an update, which led to explicit consumption-race handling.

A separate native recovery check reopened the saved parent/child after a backend restart and a versioned permission-profile change. It verified that the same child task ID and native subscription were restored without starting another model turn.

The fixture suite also exposed a shutdown race: cancellation while the ordered event consumer was draining could skip loaded-session invalidation. Cleanup now runs in a `finally` block, and the targeted disconnect/recovery regression checks pass.

The separate child-approval test passed with real Codex: a child-owned request appeared in parent polling, the main agent sent the offered cancellation decision, and Codex recorded the command as declined with the child turn interrupted. No approval was granted. Native cancellation could leave the parent's wait pending; the test explicitly interrupted that wait and sent a follow-up rather than resuming the denied child action. The parent then completed its report. This recovery path and its native-runtime limitation are documented in the examples.

That same test confirmed actual image and text ingestion: Codex identified the synthetic blue image supplied from a private snapshot and repeated the marker from the scoped UTF-8 reference file. The initial test's pending parent wait timed out; the corrected test exercising explicit parent recovery passed.

## Source archive validation — November 8, 2026

The source ZIP was built from the working source files, including files not yet committed to Git. It includes the English and Simplified Chinese READMEs, documentation, examples, runtime code and tests, with an embedded SHA-256 manifest. Git metadata, credentials, operator state, caches and installed dependencies are excluded.

A fresh copy was extracted on Linux, with every source file explicitly set to mode `0644` to simulate a Windows transfer that loses executable bits. All **61 offline tests passed** from that extracted directory. The official MCP Python **1.26.0** client check also passed from the extracted copy, including all 32 tool descriptors and human elicitation. Test fixture executables are now copied into private temporary directories and made executable there, so source file-mode handling does not prevent the tests from running on Linux.

Markdown links, JSON/TOML examples, UTF-8 Chinese text, source/ZIP byte equality and ZIP integrity were checked. This validates the transfer/package contents; it does not claim that the server itself was run under Windows or WSL. No new live model usage was needed for the documentation/source-package update.

## Current v0.1.0 source snapshot

The initial v0.1.0 source snapshot includes main-agent-selected projects, optional server configuration, native Windows named pipes/DACL checks and macOS peer/path handling. The updated Linux conversation/project suite passed 48 tests. An expanded 75-test Linux run reported one shutdown-timing failure and two platform-specific skips; the shutdown fix then passed its targeted regression. The user requested testing stop before a final full pass. Windows/macOS-specific execution and CI results are not claimed. The ZIP contains this current implementation snapshot and intentionally omits LICENSE, which the owner maintains separately.
