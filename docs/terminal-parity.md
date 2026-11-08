# Terminal controls and MCP equivalents

English | [简体中文](terminal-parity.zh-CN.md)

The implementation now exposes **32 scoped MCP tools**. It uses the installed Codex app-server protocol, including experimental fields present in the generated 0.167.0 schema. The comparison below distinguishes implemented controls from terminal-only or intentionally restricted operations. Native automatic review and native child agents were exercised with the real CLI runtime.

| Terminal workflow | MCP equivalent | Coverage |
|---|---|---|
| Start a new chat / select a project | `codex_projects`, `codex_start` | Implemented; explicit configured roots, per-task directory, persistent history |
| Resume or continue a conversation | `codex_resume`, `codex_send`, `codex_sessions` | Implemented; stopped native CLI root conversations can be imported at an exact approved cwd |
| Run multiple chats | Multiple task families | Implemented in separate projects/worktrees; prevents overlapping independent writers |
| Native subagents / `/subagents` | `codex_delegate`, `codex_subagents`, child task IDs | Implemented; parent-chain/profile verification, child events/approvals, grandchildren and recovery |
| Message/stop a native child | `codex_send`/`codex_interrupt` when supported; `codex_delegate` through parent otherwise | Native children can report that only the parent may send input; that restriction is honored |
| Interrupt or steer current work | `codex_interrupt`, `codex_send(mode="steer")` | Implemented with active-turn preconditions |
| `/permissions`, Approve for me | Start `approval_mode`; `codex_settings` | Native `auto_review` or human reviewer; existing sandbox and managed constraints remain active |
| `/model`, effort, `/fast` | `codex_models`, `codex_settings` | Model-supported efforts and tiers; selected settings can change on a running turn |
| `/plan` | `collaboration_mode: "plan"` | Native plan mode on start or subsequent turns; return to `default` for implementation |
| `/review`, `codex review` | `codex_review` | Uncommitted changes, base branch, commit or custom review |
| `/diff` | `codex_diff`, events/history | Reports the native turn diff. For an independent current Git diff, ask Codex to inspect it under its permission policy |
| `/fork`, `codex fork` | `codex_fork` | Persistent history fork, optional last included turn, goal continuation deferred |
| Rename, archive, restore, delete | `codex_rename`, `codex_archive`, `codex_delete` | Implemented; family must be idle; deletion requires exact native ID confirmation; bridge audit retained |
| `/compact` | `codex_compact` | Native asynchronous compaction, progress through normal events |
| History rollback / revert | `codex_revert` | Native paginated-history revert. Files are not reverted; confirmation explicitly acknowledges this |
| Queued follow-ups | `codex_queue` | Native add/list/update/delete/reorder/start. Idle loaded sessions may consume immediately |
| `/goal` | `codex_goal` | Native get/set/clear. Mutations require explicit user intent; budgets are never supplied implicitly |
| `/ps` / background commands | `codex_terminals` | Native list, terminate one, clean all; only the owned task's process IDs |
| `/skills` | `codex_skills`, input `skills` | Discover/reload metadata and select enabled skills by name |
| Image/file context | `images`, `context_files` in start/send/queue | In-project paths, bounded file sizes, no symlink traversal, private image snapshots |
| Structured final output | `output_schema` in start/send | Forwarded to native `turn/start.outputSchema` |
| `/status` / usage limits | `codex_status`, `codex_get`, `codex_usage`, `codex_capabilities` | Native authentication type, model usage events, rate limits and supported controls |
| `/init` and ordinary coding commands | Prompt through `codex_start`/`codex_send` | Codex performs the requested file/tool work using its normal checks |

## What is deliberately not a raw terminal replacement

The original requirements prohibit unrestricted shell/internal APIs, remote access, security bypasses and credential changes without approval. A universal CLI argument passthrough would conflict with those constraints. The server therefore exposes typed controls rather than arbitrary RPC methods or arbitrary terminal keystrokes.

| Remaining area | Investigation and reason | Available alternative |
|---|---|---|
| Arbitrary `!` shell commands, interactive terminal stdin, full-access/YOLO flags | The documented `thread/shellCommand` runs outside the sandbox and does not inherit thread permissions. Raw exec/PTY methods can similarly expand execution beyond the intended scope. They are not exposed. | Ask Codex to run a command through its normal sandboxed tools and approval flow. Use the owner-controlled native terminal for interactive programs. |
| Automatically overriding an automatic-review denial | A native override API exists, but exposing it as an ordinary model-callable action would undermine the review boundary. | Review the actual denial in native Codex and make an explicit human decision. The MCP records the native review result and never overrides it automatically. |
| Native login/logout, credential replacement and external-token refresh | Native authentication methods exist; ordinary tool arguments/results could expose secrets or mutate an existing login. | Run native login/authentication as the account owner. The bridge reuses that authorized CLI state without copying credentials. |
| Global config, hooks, plugin/MCP installation and trust changes | Native config/plugin APIs and terminal commands exist, but their effects extend beyond the approved projects and can enable code with separate trust boundaries. | Review/change those settings directly in native Codex. Existing optional integrations can be enabled for this bridge through operator configuration. Per-task models, reviewers and project access are implemented. |
| New Git worktrees / moving edits / repository cloning | Existing worktrees are selectable. Direct Git mutation can run hooks and alter shared repository metadata; raw shell passthrough was rejected. | Ask Codex to perform authorized Git work under its own checks, or create a worktree directly and select it as another project. No automatic movement of the user's dirty files occurs. |
| Native UI navigation, clipboard/editor commands, voice/realtime, debug/update utilities | These are terminal/application presentation or installation operations, not required for persistent coding conversation control. Related native APIs were inspected; none is exposed through an unrestricted escape hatch. | Use the installed CLI for those surfaces. The bridge supplies conversation content, settings, history and results over MCP. |
| Remote-control and cloud task management | The original task is same-machine and explicitly restricts enabling remote access. | Keep these in their authorized native interfaces; this service has no network listener. |

The native subagent APIs do not expose a separate public client-side spawn command in this version. The implemented alternative asks the parent to use its own native delegation tools, then verifies/subscribes to the resulting descendants. This was tested live. In particular, a child with `canAcceptDirectInput: false` is not forcibly turned into a directly controllable root agent.

Sources checked: the installed CLI help and generated experimental schemas, [official app-server documentation](https://developers.openai.com/codex/app-server), [native subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents), [automatic review](https://learn.chatgpt.com/docs/sandboxing/auto-review), and [CLI/developer command reference](https://learn.chatgpt.com/docs/developer-commands).
