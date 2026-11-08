# 集成方案调查

[English](integration-research.md) | 简体中文 · [中文首页](../README.zh-CN.md)

调查时间为 **2026 年 11 月 7 日（America/Los_Angeles）**。使用的本地 CLI 报告版本为 **codex-cli 0.167.0**，认证状态为 ChatGPT 登录。没有打开或复制认证文件。

## 检查过的依据

本地检查涵盖 `codex --help`、`exec --help`、`app-server --help`、`app-server daemon --help`、`app-server proxy --help`、`queue --help` 和 `help mcp-server`。使用安装版本生成了普通及实验性协议 schema，并在运行模型任务前验证了初始化、模型发现和命名权限配置选择。

官方文档区分了丰富的 app-server 协议、批处理执行和 SDK 工作流，并描述线程/轮次生命周期、流式事件及服务端发起的审批。具体字段以安装版本生成的 schema 为依据，真实测试用于确认本机行为。参见 [app-server 文档](https://developers.openai.com/codex/app-server)。

## 对比过的方式

| 方式 | 可行能力 | 调查结果与决定 |
|---|---|---|
| 独立 app-server stdio 连接，加私有持久后台 | 原生会话、模型目录、事件、引导/中断、问题、审批和历史 | 已选择并实现，使用生成 schema 和真实模型任务验证。后台负责生命周期、重放和客户端断连后的持续运行。 |
| 官方 Python Codex SDK | 对本地 app-server 的程序化封装，可选择已安装二进制 | 已阅读官方文档，是具有相近后端的可行方案。直接使用协议可以复用现有安装，并明确控制持久请求路由，无须额外固定另一套运行时；并未认为该 SDK 不能实现交互。 |
| 官方 TypeScript Codex SDK | 启动、继续、恢复及流式批处理任务 | 检查了文档与 `sdk/typescript/src/exec.ts`，该实现调用 `codex exec` 并传递配置，适合有界任务；不能替代所需的双向审批循环。本机当时未安装 Node。 |
| `codex exec --json` 加 `exec resume` | JSONL 进度、持久会话、结构化输出、进程取消 | 检查了安装帮助和文档，适合批处理，但没有同等的已文档化交互审批响应通道。再与 app-server 混用会增加第二套生命周期/权限路径，因此未作为静默回退。 |
| 既有 Codex daemon 加 Unix 套接字代理 | 持久共享 app-server，本地 IPC 代理 | 检查了 daemon/proxy 帮助和 Unix 传输，技术上可行。独立后台避免接管用户已有 daemon 或暴露更广的控制面；本项目不重配已有 daemon。 |
| PTY 中的原生 TUI，加 `resume`/`queue` | 人工终端交互、原生提示、排队指令 | 检查了终端参数和队列语义，可作为人工操作的替代方式。自动解析屏幕及模拟确认按键缺少明确请求 ID、类型化决定和持久事件，因此未采用。 |
| 旧 `codex mcp-server` | 曾经存在的直接 MCP 接口 | 安装版本报告该子命令不可识别，当前 SDK 文档也说明已移除并推荐 app-server。这是经过检查的结果，不是根据旧资料推测。 |
| 直接推理 API 或云端代理 | 使用正规 API 认证构建另一套代理工作流 | 不会保留安装的本地 Codex 执行环境和原生会话，本任务不需要，因此未加入推理端点调用、凭据提取、云端运行时或远程监听。 |

[SDK 官方文档](https://developers.openai.com/codex/sdk) 同时介绍两种语言 SDK 和已移除的 MCP 命令。检查的 TypeScript 源码为 [exec.ts](https://github.com/openai/codex/blob/main/sdk/typescript/src/exec.ts)，blob 为 `e1735d2c764fdba636bc2ff3bc834c13119bce3c`。批处理方案另参考 [非交互模式](https://developers.openai.com/codex/noninteractive)。

## 选定架构

```text
Claude Code / 其他本地 MCP 客户端
              | stdin/stdout 上的 MCP JSON-RPC
         server.py serve
              | 白名单操作、私有本地 IPC
         持久后台进程
              | SQLite 状态及有序事件日志
              | 子进程 stdin/stdout 上的 Codex JSON-RPC
         已安装的 codex app-server
              | 原生认证、策略与本地工具执行
         选定项目 / 已获授权的模型服务
```

stdio MCP 进程可以独立退出，后台仍持有 Codex 连接和待处理请求。多个客户端可以重新连接到同一个后台。控制操作在获得确认后返回，长时间模型工作不会占用一次很长的 MCP 调用。轮询是基础能力，进度通知和人工确认只是增强项。

MCP 实现支持 2024-11-05 至 2025-11-25 的握手协议。使用 `server/discover` 探测的客户端会得到方法不存在，然后可以走旧协议回退。已经通过官方 SDK 文档调查 MCP 2026 和 SDK v2，但尚未实现原生新版适配。应用层任务 ID 和轮询避免依赖变化中的协议任务扩展。官方 MCP Python 1.26.0 客户端互操作已测试。

stdio 消息格式遵循 [MCP 传输规范](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)。协议迁移与回退资料见 [官方 Python SDK 文档](https://py.sdk.modelcontextprotocol.io/)及[协议版本说明](https://ts.sdk.modelcontextprotocol.io/v2/protocol-versions)。

## 认证与权限选择

Codex 继续使用现有的正规本地登录。初始化时，桥接提供稳定且真实的客户端名称 `codex_local_mcp`。不会伪装官方客户端、读取令牌文件、把 ChatGPT 令牌导出到其他 API、实现外部令牌刷新回调或切换提供商。账户和工作区管理控制仍然适用。官方文档支持本地 Codex 使用 ChatGPT 或 API 密钥认证，参见 [认证文档](https://developers.openai.com/codex/auth)。

读写范围通过命名权限配置落实，仅允许必要的运行时读取和一个任务项目。普通 `workspace-write` 不被当作完整的读取隔离。会话确认中的权限配置及运行时目录必须匹配；失败会终止操作，不会切换到更宽松权限。配置只传给子进程，不修改已有配置文件、认证、钩子信任记录或策略文件。参见 [配置参考](https://developers.openai.com/codex/config-reference)。

本实现不需要使用许可不明确的凭据复用方式。未来如果涉及他人令牌、私有端点、身份伪装或不明确的自动化权限，需要另行审核并取得用户同意后再使用。

## 扩展的原生控制

扩展终端能力时再次生成并核对了安装版本协议。它提供父子关系元数据、祖先过滤、线程/轮次设置、代码审查、队列、分叉/归档/删除/回退、压缩、后台终端和目标修改。它们已映射为限定范围的工具，并对 42 种代表性消息结构完成安装 schema 检查。

该版本没有独立的公开客户端 spawn RPC，因此通过明确的委派指令让父代理调用自己的工具，再验证和订阅后代。真实子代理的发现、跟踪和后台重启后恢复已经验证。子代理的直接输入限制继续保留，由原生父代理使用代理工具控制。参见 [原生子代理](https://learn.chatgpt.com/docs/agent-configuration/subagents)。

Approve for me 通过原生 `auto_review` 审核器实现，保留 `on-request` 与受限项目配置。真实的无害提权测试产生了原生审核开始/结束事件和批准结果，没有伪造审批或绕过沙箱。参见 [自动审核](https://learn.chatgpt.com/docs/sandboxing/auto-review)。

真实队列检查发现，空闲且已加载的会话可能在后续更新之前消费新增项。因此实现会为排队工作保留项目使用权，核对原生队列/轮次状态，并报告已消费条目错误，不会再次执行同一指令。完整映射及保留的敏感控制见 [终端功能对照](terminal-parity.zh-CN.md)。
