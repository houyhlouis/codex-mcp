# 使用指南

[English](usage.md) | 简体中文 · [返回 README](../README.zh-CN.md)

本指南说明主代理（例如 Claude Code）如何通过本服务器与 Codex 协作。通常你只需用自然语言和主代理对话，工具由它替你调用。下面的 JSON 片段展示了背后发生的事情。请把 `TASK`、`TURN`、`PENDING_ID` 等占位符换成真实 ID。

- [基本流程](#基本流程)
- [问题与审批](#问题与审批)
- [后续指令、引导与中断](#后续指令引导与中断)
- [原生子代理](#原生子代理)
- [设置：模型、计划模式、审核方式、访问权限](#设置模型计划模式审核方式访问权限)
- [参考文件、图片与技能](#参考文件图片与技能)
- [审查、分叉、排队及其他控制](#审查分叉排队及其他控制)
- [重启或断开连接之后](#重启或断开连接之后)
- [获得好结果的建议](#获得好结果的建议)

## 基本流程

1. **选择项目。** 主代理选定一个绝对路径的目录。`codex_projects` 可以创建它并返回 `project_id`：

   ```json
   {"action": "create", "path": "/Users/me/projects/demo"}
   ```

   也可以把已有目录直接作为 `workdir` 传入。主目录本身、文件系统根目录以及服务器的私有文件夹会被拒绝。

2. **启动任务。** `codex_start` 会立即返回任务，Codex 在后台工作：

   ```json
   {"project_id": "PROJECT_ID", "prompt": "修复失败的 parser 测试并运行它们。", "request_id": "fix-parser-1"}
   ```

   `request_id` 是本次操作的唯一键。如果回复丢失，用相同的键和参数再调用一次，会返回已保存的结果，而不会启动第二个任务。

3. **跟踪进度。** 用上一次的游标调用 `codex_events`（从 `0` 开始）。`wait_seconds`（最多 25）会让调用等待新事件：

   ```json
   {"task_id": "TASK", "after": 0, "wait_seconds": 25}
   ```

   处理完这一页后保存返回的 `cursor`，再次调用。如果 `has_more` 为 true，立即获取下一页。每一页都会列出 `pending_requests`。

4. **处理请求。** 见[问题与审批](#问题与审批)。

5. **读取结果。** 状态变为 `completed`（或 `failed` / `interrupted`）后，`codex_get` 返回总结、修改的文件、带退出码的命令、计划和 token 用量。

每个任务就是一个持久的 Codex 对话。相关的后续工作请继续使用同一个 `task_id`。

## 问题与审批

待处理请求会出现在 `codex_events`、`codex_get` 和 `codex_requests` 中。每个请求有一个 `kind`：

| 类型 | Codex 需要什么 | 谁可以回答 |
|---|---|---|
| `parent_question` | 来自主代理的上下文或决定（`ask_parent`） | 主代理 |
| `command_approval` | 在沙箱之外运行命令的许可 | 人工；主代理只能拒绝 |
| `file_approval` | 应用补丁的许可 | 人工；主代理只能拒绝 |
| `permissions` | 本轮所需的额外权限，例如联网 | 人工；主代理只能不授予任何权限 |
| `user_input` | 需要用户回答的问题 | 人工 |
| `elicitation` | 外部 MCP 工具的表单 | 人工；主代理可以拒绝 |

**回答主代理问题**（`codex_respond`）：

```json
{"request_id": "PENDING_ID", "response": {"text": "沿用现有的 parser 接口，并运行它的单元测试。"}}
```

**获取人工决定。** 主代理调用 `codex_review_request`。如果客户端支持 MCP 表单提示，你会看到请求并做出选择；关闭提示或超时都不会批准任何内容。如果客户端不支持提示，请求会保持待处理，你可以在终端中回答：

```bash
python3 server.py review PENDING_ID
```

**拒绝**随时都可以，不需要人工：

```json
{"request_id": "PENDING_ID", "response": {"decision": "decline"}}
```

有些命令只提供 `accept` 和 `cancel`，这时用 `cancel` 表示拒绝。对于 `permissions` 请求，`{"permissions": {}, "scope": "turn"}` 表示不授予任何权限。

**Approve for me。** 启动任务时传入 `"approval_mode": "approve_for_me"`，或用 `codex_settings` 切换。之后由 Codex 自带的自动审核器决定符合条件的请求，它可以批准也可以拒绝，沙箱和项目边界保持不变。它与 `approval_relay` 设置无关，你不需要开启后者。

沉默永远不代表批准。本轮结束或 Codex 连接重启时，请求会过期。过期的请求无法再回答；请恢复任务，让 Codex 重新发起请求。

## 后续指令、引导与中断

- **后续指令**：在本轮结束后发送，可以同时更换模型：

  ```json
  {"task_id": "TASK", "prompt": "接下来增加 --json 参数并更新测试。", "request_id": "follow-1"}
  ```

- **引导**正在进行的一轮。使用 `codex_get` 返回的当前 `turn_id`：

  ```json
  {"task_id": "TASK", "prompt": "先不要管文档，专注于失败的测试。", "mode": "steer", "expected_turn_id": "TURN", "request_id": "steer-1"}
  ```

- **中断**：使用 `codex_interrupt`（`task_id`、`expected_turn_id`、`request_id`）。任务先变为 `interrupting`，Codex 确认后变为 `interrupted`。

同一目录（或其子目录）中同一时间只能有一个独立任务在运行。如需并行工作，请使用不同的目录，例如不同的 Git worktree。

## 原生子代理

用 `codex_delegate` 让父任务委派工作。子代理由 Codex 自己创建，桥接服务不会直接创建它们：

```json
{"task_id": "TASK", "prompt": "用一个子代理检查 parser，另一个检查它的测试。修改文件前先汇报发现。", "request_id": "delegate-1"}
```

如果父任务正在运行，还要传入 `expected_turn_id`。

- `codex_subagents` 列出经过验证的子代理，每个子代理都有自己的 `task_id`。
- 轮询父任务的事件时会包含子代理的事件和请求（`include_descendants` 默认为 true）。
- 只要还有子代理在工作，`family_status` 就保持 `active`。父任务这一轮结束并不代表所有子代理都已完成。
- 如果子代理的 `can_accept_direct_input` 为 false，请通过父任务用 `codex_delegate` 向它发送指令。
- 如果取消子代理的审批后父任务一直在等待，请中断父任务的这一轮，等待 `interrupted`，再发送一条说明下一步做法的后续指令。

## 设置：模型、计划模式、审核方式、访问权限

`codex_models` 列出可用的模型及其推理强度和服务等级，请只使用其中列出的组合。`codex_settings` 读取或修改任务设置：

```json
{"task_id": "TASK", "scope": "next", "collaboration_mode": "plan", "request_id": "plan-1"}
```

| 范围 | 作用 |
|---|---|
| `get` | 读取当前设置（不需要 `request_id`） |
| `next` | 作用于之后的各轮：`model`、`effort`、`approval_mode`、`collaboration_mode`、`service_tier`、`access` |
| `current` | 只作用于正在进行的一轮（需要 `expected_turn_id`）：`model`、`effort`、`approval_mode`、`service_tier` |

计划模式（`collaboration_mode: "plan"`）让 Codex 只提出计划而不实施；切换回 `default` 后再实施。`access` 可以是同一项目内的 `read` 或 `write`。`service_tier: "default"` 会清除之前选择的服务等级。

## 参考文件、图片与技能

`codex_start`、`codex_send` 和 `codex_queue` 可以接收额外输入，路径相对于项目目录：

```json
{"task_id": "TASK", "prompt": "根据规格说明和截图提出修复方案。", "context_files": ["docs/spec.md"], "images": ["screenshots/bug.png"], "request_id": "context-1"}
```

- `context_files`：最多 20 个 UTF-8 文本文件，每个 256 KiB，总计 512 KiB。
- `images`：最多 10 个 PNG、JPEG、WebP 或 GIF 文件，每个 20 MiB。Codex 读取前会先复制到私有存储中。
- `skills`：来自 `codex_skills` 的技能名称。
- `output_schema`（仅 start/send）：Codex 最终回答的 JSON Schema。

不会跟随符号链接。

## 审查、分叉、排队及其他控制

| 目的 | 工具及主要参数 |
|---|---|
| 代码审查 | `codex_review`，`target` 为 `uncommittedChanges`、`baseBranch`（加 `branch`）、`commit`（加 `commit`）或 `custom`（加 `instructions`） |
| 分叉对话 | `codex_fork`（可选 `name`、`last_turn_id`）。只复制历史，不创建 Git worktree |
| 排队后续指令 | `codex_queue`，`action` 为 `list`、`add`、`update`、`delete`、`reorder`、`start`。空闲任务可能会立即执行新加入的条目 |
| 查看差异 | `codex_diff`（Codex 报告的最近一轮差异） |
| 压缩历史 | `codex_compact`（会消耗模型额度） |
| 重命名 / 归档 / 恢复 | `codex_rename`、`codex_archive`（`action` 为 `archive` 或 `unarchive`） |
| 撤销对话轮次 | `codex_revert`，传入 `before_turn_id` 和 `confirm_history_only: true`。**不会还原文件** |
| 永久删除 | `codex_delete`，`confirm_thread_id` 必须等于任务的 `thread_id` |
| 后台进程 | `codex_terminals`（`list`、`terminate`、`clean`） |
| 持久目标 | `codex_goal`。只有在用户明确要求时才修改（`user_requested: true`） |
| 导入 CLI 会话 | `codex_sessions`（先 `list`，再用 `thread_id` 和 `confirm_stopped: true` 进行 `import`） |
| 账户额度 | `codex_usage` |

每个会改变状态的操作都需要唯一的 `request_id`。全部参数见[工具参考](tools.md)（英文）。

## 重启或断开连接之后

- **MCP 客户端断开或重启。** 不会丢失任何东西，broker 仍在运行。从保存的游标继续轮询 `codex_events` 即可。
- **broker 或 Codex 重启。** 正在进行的任务变为 `needs_resume`，待处理请求过期。调用 `codex_resume`，查看 `codex_history`，如有需要再发送后续指令。不会自动重放任何内容。
- **状态为 `uncertain`。** 某个操作的确认丢失了。重试之前先查看 `codex_get` 和 `codex_history`，不要盲目重复工作。

## 获得好结果的建议

- 给 Codex 明确的目标、预期结果以及测试方法。
- 不要在提示词或回答中放入密码、token 或密钥。
- 如实报告结果：`interrupted` 或 `failed` 的一轮不算成功。请查看 `codex_get` 中命令的退出码。
- 把 [examples/CLAUDE.md](../examples/CLAUDE.md) 复制到项目中，让主代理遵循这些规则。
