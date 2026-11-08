# Integration investigation

English | [简体中文](integration-research.zh-CN.md)

Investigated on **November 7, 2026 (America/Los_Angeles)** using the installed CLI, which reported **codex-cli 0.167.0**. Authentication status reported ChatGPT sign-in. Authentication files were not opened or copied.

## Evidence inspected

Local inspection covered `codex --help`, `exec --help`, `app-server --help`, `app-server daemon --help`, `app-server proxy --help`, `queue --help`, and `help mcp-server`. Both standard and experimental protocol schemas were generated with the installed binary. Live probes verified initialization, model discovery and named permission-profile selection before running model tasks.

The current official documentation distinguishes the rich app-server protocol from batch execution and SDK workflows. It documents thread/turn lifecycle, streamed events and server-initiated approvals. The installed schema supplied exact parameter names and response shapes; live tests established behavior on this machine. [App-server documentation](https://developers.openai.com/codex/app-server)

## Approaches compared

| Approach | Viable capabilities | Investigation and decision |
|---|---|---|
| **Dedicated app-server over stdio + private persistent broker** | Native conversations, model catalog, events, steer/interrupt, questions, approvals, persisted history | Selected and implemented. Generated schemas and live model tasks verified the integration. Broker owns lifecycle, replay and client independence. |
| **Official Python Codex SDK** | Programmatic wrapper around local app-server; can select a specific installed binary | Official docs reviewed. This is a viable alternative with essentially the same backend. Direct protocol was chosen to use the already-installed binary without adding/pinning a second runtime and to control durable request routing explicitly. It is not described as incapable of interactive integration. |
| **Official TypeScript Codex SDK** | Start/continue/resume and streamed batch tasks | Docs and official `sdk/typescript/src/exec.ts` reviewed; that implementation invokes `codex exec` and passes execution/configuration options. It is useful for bounded jobs. It does not replace the bidirectional app-server approval loop needed here. Node was not installed on this host. |
| **`codex exec --json` + `exec resume`** | JSONL progress, persisted conversations, structured final output, process cancellation | Installed help and current documentation inspected. A good batch alternative, but lacks an equivalent documented interactive approval-response channel. Combining it with app-server would add a second lifecycle/permission path without improving this implementation's requirements. Not used as a silent fallback. |
| **Existing Codex daemon + Unix-socket proxy** | Persistent shared app-server; CLI proxy can connect over local IPC | Installed daemon/proxy help and documented Unix transport inspected. Technically viable. A dedicated broker/process avoids assuming ownership of a user's existing daemon or exposing its wider thread/control surface. This project never reconfigures the existing daemon. |
| **Native TUI in a PTY + `resume`/`queue`** | Human terminal interaction, native prompts and queued instructions | CLI/TUI options and queue semantics inspected. Viable as a human-operated fallback. Automated screen scraping and synthetic approval keystrokes lack the request IDs, typed decisions and durable events of app-server; not selected. No automated approval keystrokes are used. |
| **Old `codex mcp-server` wrapper** | Former direct MCP integration | Installed `codex help mcp-server` reports an unrecognized command; current SDK docs explicitly say it was removed and direct integrations to app-server. This absence was checked, not inferred from old guidance. |
| **Direct inference APIs / cloud agents** | Could build a separate agent workflow with authorized API authentication | Does not preserve the installed local Codex harness or its native conversations. Unnecessary for this task, so no inference endpoint, credential extraction, cloud runtime, or remote listener was added. |

The [official SDK documentation](https://developers.openai.com/codex/sdk) covers both language SDKs and the removed MCP command. The TypeScript source inspected was [exec.ts](https://github.com/openai/codex/blob/main/sdk/typescript/src/exec.ts), blob `e1735d2c764fdba636bc2ff3bc834c13119bce3c`. The batch alternative was checked against [non-interactive mode](https://developers.openai.com/codex/noninteractive).

## Selected implementation

```text
Claude Code / another local MCP client
             | MCP JSON-RPC on stdin/stdout
        server.py serve
             | allowlisted operations, private Unix socket
        persistent broker
             | SQLite state + ordered event journal
             | documented Codex JSON-RPC on child stdin/stdout
        installed codex app-server
             | native auth, policy enforcement and local tool execution
        approved project / authorized model service
```

The stdio MCP process can exit independently. The broker holds the Codex connection and pending requests. Multiple clients can reconnect to that same broker. Control calls return after acknowledgements; long model work does not occupy a long MCP call. Polling is the baseline; optional progress and human elicitation are enhancements.

The MCP implementation supports the established 2024-11-05 through 2025-11-25 handshake protocols. Current clients that probe `server/discover` receive method-not-found and can use their legacy fallback. The 2026 protocol and MCP SDK v2 were investigated through current official SDK documentation; a native modern adapter is not implemented. Application task IDs and polling avoid dependency on changing protocol-level task extensions. Interoperability was tested with the official MCP Python 1.26.0 client.

The stdio framing and response behavior follow the [MCP transport specification](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports). The current SDK's protocol transition and fallback are described in the [official Python SDK documentation](https://py.sdk.modelcontextprotocol.io/) and [protocol-version documentation](https://ts.sdk.modelcontextprotocol.io/v2/protocol-versions).

## Authentication and permissions choice

Codex itself uses its existing authorized local sign-in. The bridge sends a stable, truthful client name (`codex_local_mcp`) during initialization. It does not impersonate a first-party client, read token files, export a ChatGPT token to another API, handle external-token refresh callbacks, or change providers. Native account and workspace controls remain applicable. The official documentation describes both ChatGPT and API-key authentication for local Codex. [Authentication](https://developers.openai.com/codex/auth)

The read/write distinction uses named Codex permission profiles with minimal runtime reads and a single project root. This avoids treating ordinary `workspace-write` as complete read confinement. Profiles and runtime workspace roots are verified in the thread acknowledgement; rejection is fatal rather than causing a permissive downgrade. Profiles are supplied to the child process only. Existing configuration files, authentication, hook-trust records and policy files are not modified. [Configuration reference](https://developers.openai.com/codex/config-reference)

No uncertain credential-reuse approach was needed. Any future integration that depends on a different account's tokens, a private endpoint, altered identity, or ambiguous automation entitlement needs separate review and user approval before use.

## Extended native controls

The installed protocol was regenerated and checked again when expanding terminal parity. It exposes native parent/descendant metadata, `thread/list` ancestor filters, thread/turn settings, review, queued submissions, fork/archive/delete/revert, compaction, background-terminal controls and goal mutations. These are now mapped to scoped tools, with 42 representative wire shapes checked against the installed schema.

There is no separate public client-side native spawn RPC in this version's request enum. The selected approach sends explicit delegation instructions to the parent, then verifies and subscribes to the resulting children using native ancestry and permission acknowledgements. A real child was discovered and followed successfully, including after restarting the backend. Direct-input restrictions on native children are preserved; their parent can control them using native agent tools. [Native subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents)

Approve for me is implemented by selecting the native `auto_review` reviewer while retaining `on-request` and the restricted project profile. A live harmless escalation produced native automatic-review start/completion events and an approved outcome. No fabricated approval response or sandbox bypass was used. [Automatic review](https://learn.chatgpt.com/docs/sandboxing/auto-review)

The live queue probe showed that an idle loaded conversation can consume an added item before a subsequent update. The implementation now reserves the project family for queued work, reconciles native queue/turn state, and surfaces consumed-entry errors without replaying the instruction. The [terminal parity matrix](terminal-parity.md) documents the complete mapping and excluded global/security-sensitive controls.
