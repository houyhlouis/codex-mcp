# Changelog

## 0.1.1 — 2026-11-09

Bug-fix release. After updating, run `python3 server.py stop` once so the new broker is started.

### Fixed

- **Windows CI failures.** `server.py stop` (and test teardown) returned as soon as the broker released its lock, while the process could still hold `broker.log` open. Deleting or replacing the state folder then failed with `WinError 32`. `stop` now waits until the broker process has exited.
- **Windows broker could stop accepting connections.** If a client connected and disconnected before the named-pipe listener accepted it (`ERROR_NO_DATA`), the listener thread exited and the broker became unreachable until restarted. The listener now resets the pipe and keeps listening. A pipe handle could also be closed twice during shutdown.
- **Windows state-folder ACL was rewritten on every request.** This propagated permissions through the whole state tree on each call. It is now verified on every call and rewritten only when needed.
- **`codex` could be run from the current folder.** With the default `codex_binary = "codex"`, a file named `codex` in the MCP client's working directory (often a project checkout) was executed instead of the installed CLI. On Windows, `shutil.which` also searched the current folder first. Bare names are now looked up on `PATH` only.
- **Denials rejected without a human.** The documented denial shapes `{"permissions": {}, "scope": "turn"}` and `{"action": "decline", "content": null}` were refused with "Human review required". Any validated response that grants nothing is now accepted from the main agent.
- **Relative permission paths were resolved against the broker's working directory** instead of the task's project, so valid in-project grants were refused.
- **`service_tier: "default"`** was stored and later sent to Codex as if it were a tier ID. It now clears the tier, consistently for `codex_start` and `codex_settings`.
- **Unclear error for a failed operation key.** Retrying a `request_id` whose first attempt failed printed `inspect task None`; it now includes the original error and what to do next.
- **Test fixture race.** The fake Codex wrote its history file non-atomically, so a test could read an empty file on Windows.
- Unix broker connections rejected by the peer check no longer log unhandled-exception tracebacks. Locks are released explicitly before closing.
- The elicitation form no longer fails when a Codex question has no header.
- A missing `toolchain_paths` entry now produces a clear configuration error.

### Changed

- `stop` reports `{"stopped": true, "pid": ...}` after the broker has exited.
- A clearer error explains how to stop a broker left over from a different version or configuration.
- CLI: `--version` and help text for every command.
- CI: byte-compile step, package-install smoke test, and a source-archive check that no longer hard-codes the version.
- Documentation rewritten: README with a full installation guide, plus a smaller set of focused guides. Validation records, terminal-parity and research notes were merged into the architecture, limitations and development pages.

## 0.1.0 — 2026-11-08

Initial source release.
