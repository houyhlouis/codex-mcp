# Native platforms

English | [简体中文](platforms.zh-CN.md)

The implementation has native adapters for Windows, macOS and Linux. WSL is not required for the Windows adapter.

| Platform | Local transport and protection |
|---|---|
| Windows | Named pipes with an explicit user/System DACL, remote clients rejected, and peer-process SID/restricted-token checks |
| macOS | Private Unix sockets, `getpeereid` peer verification, and normalization of root-owned `/tmp`, `/var` and `/etc` aliases |
| Linux | Private Unix sockets, kernel peer credentials and Unix ownership/mode checks |

All platforms use UTF-8 stdio for MCP, a detached local broker, explicit project selection and native Codex permission profiles. Native Windows executable discovery resolves supported npm launchers to `codex.exe` without invoking a command shell. File input handling rejects symlinks/reparse points and snapshots images into private state.

## Requirements

Use Python 3.11+ and a working native Codex CLI 0.167.0+ under the same account as the main agent. Complete Codex's normal sandbox setup on the selected OS. The bridge does not disable native security controls. The legacy Windows `unelevated` sandbox is rejected because it does not enforce denied reads; use an available native MXC/elevated mode allowed by your environment.

For an MCP host that kills all descendant processes on exit, start the broker from a separate terminal with `python server.py start` before connecting the client. The bridge does not override operating-system job restrictions.

## Validation status

Development and executed regression checks took place on Linux. Windows/macOS adapters and native-specific tests are included, with a CI matrix targeting all three platforms, but their native execution was not available in this development session. Testing was stopped at the user's request before a final full-platform pass. See [validation](validation.md) for the earlier tested baseline and current scope.

Sources: [native Codex Windows sandbox](https://learn.chatgpt.com/docs/windows/windows-sandbox), [Windows named-pipe security](https://learn.microsoft.com/en-us/windows/win32/ipc/named-pipe-security-and-access-rights), and [Apple getpeereid documentation](https://developer.apple.com/library/archive/documentation/System/Conceptual/ManPages_iPhoneOS/man3/getpeereid.3.html).
