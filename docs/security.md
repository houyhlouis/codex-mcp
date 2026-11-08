# Security and trust boundaries

English | [简体中文](security.zh-CN.md)

## Local ownership

This is a same-user local integration, not a multi-user service. MCP uses inherited stdin/stdout. The broker uses private Unix sockets on Linux/macOS and native named pipes on Windows, never TCP/HTTP. Unix ownership/modes or Windows DACLs protect state, and peer identities are verified. Windows pipes reject remote and restricted-token clients. No listener address or remote-control setting is exposed.

The broker accepts only named tool operations, a health check, local human-review responses and shutdown. There is no raw JSON-RPC proxy to Codex and no public shell/file-read tool. The Codex executable is chosen in the operator-owned configuration and launched with an argument array, never a shell command built from a prompt.

Processes already running as the same OS user can access that user's files and invoke that user's programs. Socket permissions and the review route do not distinguish a human from a fully trusted same-user process. The threat model is accidental misuse, prompt/tool argument injection, unauthorized path selection and untrusted remote callers—not a hostile administrator or same-user process with unrestricted shell access.

## Projects and execution

Project directories are explicitly chosen or created by the trusted main agent. No workspace list is stored in configuration. The selection grants that task access to the selected project. Paths are checked for traversal, symlinks and Windows reparse points; broad roots and private state/credential directories are rejected. Independent active task families cannot overlap. Verified native descendants may collaborate within their parent project.

Each Codex conversation selects a named permission profile with:

- The selected project as the only runtime workspace root.
- `read` or `write` project access, selected at task creation.
- Codex's minimal platform runtime reads, read access to the exact installed Codex executable, and any operator-configured read-only toolchain paths.
- Denial of access to this bridge's state directory and operator configuration.
- Command network access disabled until native policy/approval permits a requested exception.

The bridge verifies the acknowledged profile ID, working directory, runtime workspace roots and approval policy before sending work. It does not downgrade to full access, disable the sandbox, bypass hook trust, ignore policy rules, or edit managed requirements. Unsupported/rejected profiles fail closed.

These profiles govern Codex's sandboxed tools. They are not a container around every process in the user account. Codex's own trusted runtime must access its authentication/configuration and contact its model service. Native trusted hooks and any explicitly enabled external tools have their own trust boundaries. Network access for model inference, web search or an external connector is different from sandboxed command network access.

External MCP servers and apps/plugins are disabled for bridge threads by default. `allow_external_tools = true` is an operator opt-in to use existing authorized integrations; it does not make them obey a project filesystem sandbox. It can also reintroduce recursive MCP delegation if the user's Codex configuration points back at this bridge. Review those integrations and their permissions before opting in.

## Approval routing

Codex remains the policy enforcement engine. The bridge records its requests and forwards supported responses; it cannot make a forbidden profile or account entitlement valid.

Default behavior is:

| Request | Default responder |
|---|---|
| `ask_parent` task-context question | Main agent can respond |
| Command/file approval | Human UI/terminal; main agent can deny/cancel |
| Additional permissions | Human UI/terminal; main agent can grant nothing |
| Native user-input question | Human UI/terminal, since some connector approvals use this shape |
| External MCP elicitation | Human UI/terminal; main agent can decline/cancel |
| Unknown request method | Explicit error, no grant |

Tasks can explicitly choose native `approve_for_me`, which routes eligible requests through Codex's own automatic reviewer. This is selected with `approvalsReviewer: "auto_review"` plus `approvalPolicy: "on-request"`, while keeping the restricted named project profile. The server verifies the selected reviewer on creation/resumption and records native automatic-review events. It never manufactures an approval response to imitate this mode. Managed requirements can reject the selection. Per-task changes do not modify global credentials or configuration.

Native child ownership is verified from app-server metadata, not from a task prompt or arbitrary model-supplied thread ID. The parent chain must reach an owned task, the child's cwd must remain within the parent project, and its resumed permission profile/workspace roots must match. The broker subscribes to verified children and rechecks them after a backend restart. Requests retain originating child and parent/root IDs. Unrelated or outside-project children are not adopted. Children that cannot accept direct input remain controlled through their native parent.

Explicit import of a stopped native root conversation is supported only at the exact selected approved cwd. The caller must confirm it is stopped in other clients, because a separate app-server's active process is not reliably visible. Imported work receives the bridge's restricted profile before a new turn. Archive/delete and history rollback are scoped to owned families; deletion requires exact native-thread confirmation, and history rollback explicitly acknowledges that files are not reverted. These confirmations record caller intent, not independent proof of a human action.

`approval_relay = true` permits the main agent to relay supported decisions. It is configured outside the model's tools and requires a broker restart. It is a delegation of trust, not proof of a human click. The human-prompt adapter requires an explicit accepted elicitation response; defaults, timeout, cancellation and lack of client support never approve a request.

`codex_respond` exposes only one-shot approvals. No `acceptForSession`, command-rule amendments, persistent network rules, or session permission grants. File approval reviews include the proposed patch when available and cannot grant outside-project filesystem access. Extra permission grants are limited to requested subsets and concrete in-project paths. Native automatic review follows Codex's own policy and can approve or deny an escalation. A reviewed escalation can run with broader native privileges; published native review records identify its outcome. There is no blanket escalation mode or automatic override of a review denial.

Pending requests are tied to the live backend generation and task. They expire on disconnect, restart or turn completion. A reply after expiration is rejected. The bridge never auto-answers a model question after `autoResolutionMs` or silently restores a former approval.

## Credentials and retained data

The bridge does not open Codex authentication files, export tokens, change sign-in, log out, or implement an alternate authentication flow. `account/read` is reduced to sign-in type and presence; identity fields and credentials are not exposed. Normal authentication/refresh remains owned by the installed Codex process.

Do not place credentials in prompts, answers, command output or project files you intentionally expose to a coding agent. Secret-marked questions are not accepted by this bridge; use native authentication/provider browser flows. The server does not fetch authorization URLs or tokens on the user's behalf.

The SQLite journal contains prompts, code diffs, progress and command output. Common token/key patterns and sensitive field names are redacted before storage/return, and raw reasoning is excluded. Pattern redaction cannot identify every possible secret in arbitrary text. Treat the entire state directory and Codex's separate history as sensitive local data. They are not encrypted by this application.

Reference-file ingestion uses directory descriptors and refuses symlink components, nonregular files and oversized input. Image bytes are read from the approved project and copied to private state for native ingestion, preventing a writable project path from being swapped to a credential file after validation. Snapshots remain in state until the operator retires it. Ordinary context text and selected skill instructions are untrusted content, not permission to change the project scope.

Response bookkeeping records only request ID/kind and review origin, not answer contents. Codex can subsequently echo nonsecret answers into its item/history events, which are journaled under the normal redaction rules. This is another reason authentication secrets must use dedicated authentication flows.

The store records explicit project selections. Saved task paths are revalidated before use; previous task directories can be migrated into the registry. There is no automatic event deletion. Stop the broker before manually retiring/backing up its state.
