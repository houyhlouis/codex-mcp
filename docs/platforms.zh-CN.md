# 原生平台支持

[English](platforms.md) | 简体中文 · [中文首页](../README.zh-CN.md)

实现中包含 Windows、macOS 和 Linux 的原生适配。Windows 适配不依赖 WSL。

| 平台 | 本地通信与保护措施 |
|---|---|
| Windows | 使用带有明确用户/System DACL 的命名管道，拒绝远程连接，并检查对端进程 SID 和受限令牌 |
| macOS | 使用私有 Unix 套接字、`getpeereid` 对端身份校验，并规范化由 root 拥有的 `/tmp`、`/var`、`/etc` 系统别名 |
| Linux | 使用私有 Unix 套接字、内核提供的对端凭据，以及 Unix 所有者和权限位检查 |

各平台均使用 UTF-8 标准输入输出传输 MCP 消息，采用独立后台进程、明确的项目选择和 Codex 原生权限配置。Windows 可执行文件发现逻辑会把支持的 npm 启动器解析为实际的 `codex.exe`，不会通过命令 Shell 启动它。文件输入会拒绝符号链接和重解析点，并将图片复制到私有状态目录作为快照。

## 环境要求

使用 Python 3.11+ 和可用的原生 Codex CLI 0.167.0+，并与主代理使用同一个账户。应在所选系统上完成 Codex 的正常沙箱配置，桥接服务不会关闭原生安全控制。

Windows 的旧式 `unelevated` 沙箱无法落实禁止读取规则，因此服务会拒绝该模式；应使用环境允许且可正常工作的原生 MXC 或 `elevated` 模式。

如果 MCP 宿主退出时会终止所有后代进程，可以先在独立终端运行 `python server.py start`，再连接客户端。桥接服务不会绕过操作系统的作业对象限制。

## 验证状态

开发及已经执行的回归检查位于 Linux 环境。项目包含 Windows/macOS 适配、原生平台专用测试，以及面向三个平台的 CI 矩阵，但当前开发会话无法在 Windows 或 macOS 上实际执行这些测试。按用户要求，最终完整平台验证前已经停止测试。此前的验证基线及当前范围见 [验证记录](validation.zh-CN.md)。

参考资料：[Codex 原生 Windows 沙箱](https://learn.chatgpt.com/docs/windows/windows-sandbox)、[Windows 命名管道安全](https://learn.microsoft.com/en-us/windows/win32/ipc/named-pipe-security-and-access-rights)、[Apple getpeereid 文档](https://developer.apple.com/library/archive/documentation/System/Conceptual/ManPages_iPhoneOS/man3/getpeereid.3.html)。
