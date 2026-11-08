# Configuration

English | [简体中文](configuration.zh-CN.md)

No configuration file is required. The main agent chooses a project through `codex_projects` or supplies an absolute `workdir` to `codex_start`. There is no configured workspace list.

```json
{"action":"create","path":"C:\\Work\\demo"}
```

Use the returned `project_id` when starting a task, or supply an existing directory:

```json
{"workdir":"/Users/me/projects/demo","prompt":"Implement the requested feature and run tests.","request_id":"feature-001"}
```

Each task is restricted to its selected project. The server rejects filesystem roots, the whole home directory, private state/credential directories, symlinks and Windows junctions. macOS system aliases such as `/tmp` are normalized. A trusted main agent is responsible for selecting the intended project; directory selection is an explicit access grant.

## Optional settings

Pass `--config /absolute/settings.toml` before the command if defaults need changing. See [config.example.toml](../config.example.toml).

| Setting | Purpose |
|---|---|
| `codex_binary` | Native Codex executable or command on PATH |
| `state_dir` | Private broker state; defaults to `%LOCALAPPDATA%/CodexMCP` on Windows and `~/.local/state/codex-mcp` on Unix |
| `max_tasks` | Independent task-family concurrency, 1–32; default 4 |
| `toolchain_paths` | Additional operator-approved read-only runtime paths |
| `approval_relay` | Allow trusted main-agent approval responses; default false |
| `allow_external_tools` | Allow existing external integrations; default false |

Store a real settings file outside directories selected for coding tasks. Native Approve for me is a per-task `approval_mode`, independent of `approval_relay`.

## Earlier development configurations

Stop the old broker with its previous settings file, remove `[workspace_roots]`, and restart. Replace old tool arguments `root`/relative `path` with `workdir` or `project_id`. Previously recorded task directories are migrated into the project registry and revalidated before use. No prompts are automatically replayed.
