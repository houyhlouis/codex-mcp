# 终端控制与 MCP 对照

[English](terminal-parity.md) | 简体中文 · [中文首页](../README.zh-CN.md)

当前实现提供 **32 个限定范围的 MCP 工具**，使用已安装 Codex 的 app-server 协议，包括 0.167.0 生成 schema 中的实验性字段。下表区分已实现的控制、仍需原生终端的功能和有意保留的限制。原生自动审核和原生子代理已在真实 CLI 运行时中验证。

| 终端工作流 | MCP 对应方式 | 覆盖范围 |
|---|---|---|
| 新建会话/选择项目 | `codex_projects`、`codex_start` | 主代理明确选择或创建目录，每个任务单独指定项目，并保留会话历史 |
| 恢复或继续会话 | `codex_resume`、`codex_send`、`codex_sessions` | 可以导入已停止、工作目录与所选项目完全一致的原生根会话 |
| 多个会话并行 | 多个任务组 | 可在不同项目/worktree 中运行，阻止独立任务写入重叠目录 |
| 原生子代理/`/subagents` | `codex_delegate`、`codex_subagents`、子任务 ID | 验证父链和权限，汇总子代理事件/审批，支持后代和恢复 |
| 向子代理发消息或停止它 | 原生允许时用 `codex_send`/`codex_interrupt`，否则经父代理调用 `codex_delegate` | 尊重子代理仅允许父代理输入的限制 |
| 引导或中断当前工作 | `codex_interrupt`、`codex_send(mode="steer")` | 要求当前活动轮次匹配 |
| `/permissions`、Approve for me | 创建时的 `approval_mode`、`codex_settings` | 选择原生 `auto_review` 或人工审核器，保留沙箱和管理员约束 |
| `/model`、推理强度、`/fast` | `codex_models`、`codex_settings` | 使用模型支持的推理强度和档位，部分设置可作用于当前轮次 |
| `/plan` | `collaboration_mode: "plan"` | 创建或后续轮次使用原生计划模式，实现时切回 `default` |
| `/review`、`codex review` | `codex_review` | 未提交变更、基准分支、指定提交或自定义审查 |
| `/diff` | `codex_diff`、事件/历史 | 返回原生轮次差异；需要独立检查当前 Git 差异时，可让 Codex 在其权限规则下执行 |
| `/fork`、`codex fork` | `codex_fork` | 持久历史分叉，可选择最后包含的轮次，并推迟目标的自动继续 |
| 重命名、归档、恢复、删除 | `codex_rename`、`codex_archive`、`codex_delete` | 要求任务组空闲；删除需确认准确的原生 ID，保留桥接审计记录 |
| `/compact` | `codex_compact` | 原生异步压缩，通过普通事件返回进度 |
| 历史回退 | `codex_revert` | 原生分页历史回退；不撤销文件，并要求明确确认这一点 |
| 排队的后续指令 | `codex_queue` | 原生添加/列表/更新/删除/排序/启动；空闲的已加载会话可能立即消费 |
| `/goal` | `codex_goal` | 原生读取/设置/清除；修改需明确用户意图，不会隐式添加预算 |
| `/ps`、后台命令 | `codex_terminals` | 查看、停止单个或清理全部后台终端，仅限所属任务的进程 ID |
| `/skills` | `codex_skills`、输入中的 `skills` | 发现/刷新元数据，并按名称选择启用的技能 |
| 图片/文件上下文 | start/send/queue 中的 `images`、`context_files` | 项目内路径、大小限制、禁止符号链接遍历、私有图片快照 |
| 结构化最终输出 | start/send 中的 `output_schema` | 转交给原生 `turn/start.outputSchema` |
| `/status`、用量限额 | `codex_status`、`codex_get`、`codex_usage`、`codex_capabilities` | 原生认证类型、模型用量事件、限额和支持的控制项 |
| `/init` 和普通编程命令 | 通过 `codex_start`/`codex_send` 提出请求 | Codex 使用正常检查机制执行文件或工具操作 |

## 不提供任意终端透传

原始要求禁止无限制 Shell/内部 API、远程接入、安全绕过，以及未经同意的凭据更改。任意 CLI 参数透传会与这些边界冲突，因此本服务提供有明确参数类型的控制，而不是通用 RPC 或终端按键代理。

| 仍保留的边界 | 调查结果及原因 | 可用替代方式 |
|---|---|---|
| 任意 `!` Shell、交互终端标准输入、full-access/YOLO 参数 | 文档中的 `thread/shellCommand` 在沙箱外运行，不继承线程权限；原始 exec/PTY 也可能超出预期范围，因此不开放。 | 让 Codex 通过正常沙箱工具和审批流程运行命令；交互程序使用账户所有者控制的原生终端。 |
| 自动推翻自动审核拒绝 | 原生覆盖 API 存在，但作为普通模型工具开放会削弱审核边界。 | 在原生 Codex 中审阅拒绝并由人明确决定。MCP 记录审核结果，不自动覆盖。 |
| 原生登录/退出、替换凭据、外部令牌刷新 | 原生认证接口存在，但普通工具参数或结果可能暴露秘密或改变已有登录。 | 由账户所有者完成原生认证，桥接复用正规 CLI 状态而不复制凭据。 |
| 全局配置、钩子、插件/MCP 安装和信任变更 | 相关原生 API/命令存在，但影响可能超出选定项目，并启用具有独立信任边界的代码。 | 在原生 Codex 中审阅和更改；已有可选集成可由操作者配置启用。任务级模型、审核器和项目权限已实现。 |
| 新建 Git worktree、移动改动、克隆仓库 | 已有 worktree 可作为项目选择；直接 Git 修改可能运行钩子并影响共享元数据，因此没有提供原始 Shell 透传。 | 让 Codex 在正常检查下执行授权的 Git 工作，或自行创建 worktree 后选择它。服务不会自动移动用户尚未整理的改动。 |
| 原生界面导航、剪贴板/编辑器命令、语音/实时模式、调试/更新工具 | 属于终端/应用展示或安装操作。已检查相关原生接口，但未作为无限制入口开放。 | 使用已安装 CLI 的相应界面；MCP 提供会话内容、设置、历史和结果。 |
| 远程控制和云端任务管理 | 本任务要求同机运行，并明确限制启用远程访问。 | 使用各自获得授权的原生界面；本服务没有网络监听端口。 |

当前版本没有单独公开给客户端的原生子代理 spawn RPC。已实现的方式是明确要求父代理使用自己的委派工具，再验证并订阅产生的后代，这条流程已经实测。对于 `canAcceptDirectInput: false` 的子代理，不会强行把它变成可直接控制的根代理。

参考资料：已安装 CLI 的帮助与生成 schema、[官方 app-server 文档](https://developers.openai.com/codex/app-server)、[原生子代理](https://learn.chatgpt.com/docs/agent-configuration/subagents)、[自动审核](https://learn.chatgpt.com/docs/sandboxing/auto-review)、[CLI/开发者命令参考](https://learn.chatgpt.com/docs/developer-commands)。其他边界见 [已知限制](limitations.zh-CN.md)。
