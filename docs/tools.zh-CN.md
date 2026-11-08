# MCP 工具参考

[English](tools.md) | 简体中文 · [中文首页](../README.zh-CN.md)

所有工具参数均为 JSON 对象，未知字段会被拒绝。响应在文本内容块中提供 JSON；MCP 2025-06-18 及后续兼容版本还提供一致的 `structuredContent`。操作错误使用 `isError: true`，格式不正确的工具参数返回 JSON-RPC `-32602`。

服务不提供任意 Shell、任意文件读取、全局配置写入、登录/退出、令牌访问或原始 app-server RPC 透传工具。

## 工具列表

| 工具 | 必需参数 | 可选参数与默认值 | 返回值/用途 |
|---|---|---|---|
| `codex_status` | 无 | 无 | CLI 版本、后台类型、登录类型、待处理请求、日志最高序号；按需启动 Codex |
| `codex_projects` | 列表无需额外参数；选择/创建需要绝对 `path` | `action: list`、`path`、`offset: 0`、`limit: 100` | 已选择的项目，或新选定/创建的项目描述符 |
| `codex_models` | 无 | `cursor`、`limit: 100` | 实时模型目录页、支持/默认推理强度和 `nextCursor` |
| `codex_start` | `prompt`、`request_id`；`workdir` 与 `project_id` 必须且只能提供一个 | `model`、`effort`、`access: write`、`approval_mode: inherit`、`collaboration_mode: default`、`service_tier`、`name`、下文的扩展输入 | 创建持久任务并确认首轮启动 |
| `codex_list` | 无 | `offset: 0`、`limit: 50` | 按新到旧排列的任务快照和 `next_offset` |
| `codex_get` | `task_id` | 无 | 当前状态、最新摘要、文件、命令、计划、用量和请求 |
| `codex_events` | 无 | `task_id`、`after: 0`、`limit: 100`、`wait_seconds: 0`、`include_descendants: true` | 持久事件、下一页 `cursor`、`has_more` 和待处理请求 |
| `codex_send` | `task_id`、`prompt`、`request_id` | `mode: follow_up`、`expected_turn_id`、`model`、`effort`、扩展输入 | 启动后续轮次，或引导当前轮次 |
| `codex_resume` | `task_id`、`request_id` | 无 | 加载保存的会话并核对最近轮次状态，不重放提示 |
| `codex_interrupt` | `task_id`、`expected_turn_id`、`request_id` | 无 | 确认中断请求；仍需轮询直到原生轮次真正结束 |
| `codex_history` | `task_id` | `kind: turns`、`cursor`、`limit: 50`、`turn_id` | 带 `nextCursor` 的 Codex 历史页；`kind` 可为 `turns` 或 `items` |
| `codex_requests` | 无 | `task_id` | 待处理问题/审批，不包含已解决或过期的请求 |
| `codex_respond` | `request_id`、`response` | 无 | 校验并回答仍然有效的待处理请求 |
| `codex_review_request` | `request_id` | 无 | 使用客户端人工确认界面，或返回继续等待/本地审核的替代路径 |
| `codex_capabilities` | `task_id` | 无 | 原生协作模式、权限配置目录及是否接受直接输入 |
| `codex_settings` | `task_id`；修改时需要 `request_id` | `scope: get`（`get/next/current`）、`expected_turn_id`、`model`、`effort`、`approval_mode`、`collaboration_mode`、`service_tier`、`access` | 查看或修改设置；当前轮次修改不会改变后续轮次 |
| `codex_subagents` | `task_id` | `refresh: true`、`cursor`、`limit: 50`、`archived: false` | 发现、验证、订阅原生后代并返回子任务 ID |
| `codex_delegate` | `task_id`、`prompt`、`request_id` | 父代理正在运行时需要 `expected_turn_id` | 请求父代理创建或控制原生子代理 |
| `codex_fork` | `task_id`、`request_id` | `name`、`last_turn_id` | 分叉原生历史，不启动工作，也不自动继续目标 |
| `codex_rename` | `task_id`、`name`、`request_id` | 无 | 重命名原生会话 |
| `codex_archive` | `task_id`、`request_id` | `action: archive`（`archive/unarchive`） | 归档或恢复空闲任务组 |
| `codex_delete` | `task_id`、`confirm_thread_id`、`request_id` | 无 | 删除原生历史及后代，要求确认准确的原生 ID；保留桥接审计记录 |
| `codex_compact` | `task_id`、`request_id` | 无 | 启动原生上下文压缩，随后轮询事件；可能消耗模型用量 |
| `codex_revert` | `task_id`、`before_turn_id`、`confirm_history_only: true`、`request_id` | 无 | 移除指定轮次及之后的原生历史，不撤销文件 |
| `codex_queue` | `task_id`；修改时需要 `request_id` | `action: list`（`list/add/update/delete/reorder/start`）、`prompt`、`queued_id`、`ordered_ids`、`cursor`、`limit: 50`、图片/上下文文件/技能 | 管理原生待执行指令；添加可能立即触发执行 |
| `codex_review` | `task_id`、`request_id` | `target: uncommittedChanges`（`uncommittedChanges/baseBranch/commit/custom`）、`branch`、`commit`、`instructions` | 在当前会话启动原生代码审查轮次 |
| `codex_terminals` | `task_id`；修改时需要 `request_id` | `action: list`（`list/terminate/clean`）、`process_id`、`cursor`、`limit: 50` | 查看或停止所属任务的后台进程，不执行任意 Shell |
| `codex_goal` | `task_id`；修改时需要 `request_id` 和 `user_requested: true` | `action: get`（`get/set/clear`）、`objective`、`status`、`token_budget` | 原生持久目标，不自动填入预算或推断用户意图 |
| `codex_skills` | `task_id` | `force_reload: false` | 当前项目可用的技能元数据；输入中使用已启用且唯一的名称 |
| `codex_diff` | `task_id` | 无 | 最近原生轮次的差异和文件列表，不是独立的 Git 工作区审计 |
| `codex_usage` | 无 | 无 | 原生账户限额/额度信息，不改变计费 |
| `codex_sessions` | `workdir` 或 `project_id`；导入时还需 `thread_id`、`confirm_stopped: true`、`request_id` | `action: list`（`list/import`）、`cursor`、`limit: 50`、`archived: false` | 在准确选定的项目工作目录内发现或导入原生根会话 |

32 个工具共用明确的调用白名单。一个工具即使允许不带操作键的只读动作，其修改动作仍必须提供操作键。

新任务的 `approval_mode` 可以是 `inherit`、`ask` 或 `approve_for_me`；修改设置时选择 `ask` 或 `approve_for_me`，对应原生 `user`/`auto_review` 审核器。自动审核可以同意或拒绝，其公开审核事件和最新结果会被保存，并不代表无限制访问。`collaboration_mode` 为 `default` 或 `plan`。服务档位必须由所选模型提供；设置中的 `default` 用于重置档位。访问范围仍是同一项目中的 `read` 或 `write`。当前轮次修改需要 `expected_turn_id`，且不能改变访问范围或协作模式。

`start/send` 的扩展输入包括：`images`（最多 10 个项目相对路径）、`context_files`（最多 20 个 UTF-8 文件）、`skills`（最多 10 个已启用的唯一技能名），以及 `output_schema`（只适用于新轮次的原生最终输出约束）。每个文本文件最多 256 KiB，总计最多 512 KiB；每张图片最多 20 MiB。读取时不跟随符号链接；图片先复制到私有状态目录，再交给原生接口，避免项目进程在校验后替换路径。这些快照随状态保留。队列新增/修改支持上述输入，但不支持最终输出约束。

每页为 1–200 条记录，每次轮询最多等待 25 秒，单条提示最多 100,000 个字符。事件页另有约 1 MiB 的大小预算，单个更大的事件会完整返回。Codex 消息帧上限为 16 MiB，后台响应上限为 32 MiB；超出时会停止相关连接，不会悄悄截掉一部分消息。

模型和推理强度应使用 `codex_models` 返回的准确组合。目录中列出的模型仍可能因账户权限而无法调用，应把失败告知用户。可以在新的后续轮次中切换模型，不能通过普通 `steer` 切换；支持的当前轮次设置应使用 `codex_settings`。

项目由主代理选择或创建，不需要工作区配置。每次任务明确指定绝对 `workdir` 或已返回的 `project_id`；`codex_projects(action="select")` 不会设置进程级全局当前项目。创建操作只创建普通目录，不克隆仓库、不初始化 Git、不创建 worktree，也不安装依赖。

## 任务与轮次语义

任务 ID 对应桥接服务管理的持久会话；`thread_id` 对应其原生 Codex 会话。初始指令及后续指令会在该会话中启动新轮次。

当任务本身或已知后代仍在运行、排队或恢复时，`family_status` 为 `active`，否则为 `idle`。父轮次完成不代表所有子工作已经结束，还要查看子任务和待处理请求。

| 状态 | 含义 |
|---|---|
| `starting` | 正在等待控制操作确认 |
| `queued` | 原生待执行工作为该任务组保留项目使用权 |
| `running` | 有活动轮次 |
| `waiting` | Codex 请求正在等待回答 |
| `interrupting` | 中断请求已确认，尚未确认实际结束 |
| `completed` | 最近轮次成功完成 |
| `failed` | 创建任务或最近轮次失败，应检查错误和事件 |
| `interrupted` | Codex 已确认中断 |
| `needs_resume` | 后台/Codex 实时连接丢失，仍可获取保存的历史 |
| `uncertain` | 控制确认丢失，不要盲目重复执行 |
| `idle` | 保存的会话已加载，且未确认有活动轮次 |
| `archived` | 继续会话前需要恢复归档 |
| `deleted` | 原生历史已删除，桥接审计记录仍保留 |

恢复后的空闲任务可能带有 `recovered_turn_status`，用于描述最近持久化轮次。恢复条目的权威内容应查询 `codex_history`。恢复会话不会自动重启被中断的命令，也无法补回全部流式增量。

一个项目或重叠目录只允许一个独立活动任务组。验证通过的原生后代可以在组内协作。配置的并发上限统计活动根任务组；Codex 自行控制内部代理并发。不同同级目录中的既有 worktree 可以作为独立项目选择；桥接不管理 Git worktree 生命周期。

`summary` 是代理最新撰写的消息，任务运行中可能只是阶段性说明。`files` 来自文件变更条目报告。`commands` 保留最近 100 条命令及退出码，完整记录和输出仍可从事件/历史查询。Shell 命令退出成功不等于所有要求的测试都通过，应同时阅读输出及最终报告。

## 重放与重试

`after` 是排他性的、单调递增的本地日志序号，不是时间戳或 Codex 历史游标。处理完当前页后再推进到返回的 `cursor`。`has_more` 为真时立即读下一页。不要把某个任务过滤器的游标直接用作全局事件流游标。

事件包含代理文本增量、完成条目、命令输出、变更、计划、用量、自动审核结果及后台生命周期信息。父任务默认包含已验证的后代，每条事件保留原始 `task_id`。原始私有推理内容会被排除；可用的推理摘要可以保留。`bridge/request` 带有稳定的待处理请求 ID 和父/根任务 ID，同一请求也会出现在轮询返回值中，因此通知丢失不会隐藏它。

`start/send/resume/interrupt` 及其他修改操作中的 `request_id` 是**操作幂等键**。相同参数重复调用会返回已保存的确认，其快照可能早于 `codex_get`。同一键用于不同参数会失败。结果不确定的键保持不确定状态，不会引发自动重放。

响应/人工审核工具中的 `request_id` 则是服务返回的**待处理 Codex 请求 ID**。已解决、过期、中断或属于旧进程的请求不能继续回答；传输失败后也不会自动重发答案。

## 响应格式

对于 `parent_question`，主代理可以直接补充任务上下文：

```json
{"request_id":"PENDING_ID","response":{"text":"保留现有解析器接口，并运行对应的单元测试。"}}
```

对于 `command_approval` 和 `file_approval`，只能提交原生接口提供的决定之一：

```json
{"request_id":"PENDING_ID","response":{"decision":"accept"}}
```

支持 `accept`、`decline`、`cancel`，但必须属于 Codex 的 `availableDecisions`。部分命令只提供 `accept` 和 `cancel`，没有 `decline`；此时用 `cancel` 拒绝，可能同时中断轮次。`acceptForSession`、策略修订和持久网络规则修改都会被拒绝。文件审批会在 Codex 提供时附带拟应用的补丁，并且必须位于当前项目内。

对于内置 `user_input`，需要回答所有问题 ID：

```json
{
  "request_id":"PENDING_ID",
  "response":{"answers":{"choice":{"answers":["使用当前的数据结构。"]}}}
}
```

对于 `permissions`，只能授予本次请求权限的子集，且限定于当前轮次：

```json
{
  "request_id":"PENDING_ID",
  "response":{"permissions":{"network":{"enabled":true}},"scope":"turn"}
}
```

空的 `permissions` 对象不授予任何权限。文件系统授权必须是当前项目内的具体路径；特殊路径、通配符和会话级授权不开放。桥接会拒绝请求中没有提出的额外权限。

外部 MCP 的 `elicitation` 请求可使用受支持的简单表单：

```json
{"request_id":"PENDING_ID","response":{"action":"accept","content":{"choice":"yes"}}}
```

`decline`/`cancel` 会发送 `content: null`。URL 模式只有在用户完成提供商流程后，才可用 `action: "accept", content: null` 转发完成结果。桥接不会访问该 URL 或获取令牌。高级 schema 不受支持时会拒绝继续；秘密字段应通过原生/提供商认证流程输入，而不是 MCP 参数。

除上下文问题和拒绝决定外，上述响应需要 `codex_review_request` 的人工界面、交互终端审核，或操作者明确启用 `approval_relay = true`。`response` 是对应请求类型的数据，不是任意 app-server 方法。

## 从 CLI 调用工具

诊断 CLI 使用同样的工具白名单和校验。从源码根目录执行：

```bash
python3 server.py call codex_events --args '{"task_id":"TASK_ID","after":0,"wait_seconds":25}'
```

使用自定义设置时，在 `call` 前加 `--config /absolute/settings.toml`。`--args -` 可从标准输入读取 JSON，避免出现在进程参数列表中。不要通过任务提示或回答传递凭据。
