# 故障排除

[English](troubleshooting.md) | 简体中文 · [返回 README](../README.zh-CN.md)

请在 `codex-mcp` 文件夹中运行以下命令。Windows 上用 `py -3` 代替 `python3`。如果使用了配置文件，请在命令前加上 `--config /path/config.toml`。

## 先做这些检查

```bash
python3 --version              # 3.11 或更新
codex --version                # 0.167.0 或更新
codex login status             # 已登录
python3 server.py --version
python3 server.py status       # 启动 broker；应显示 "signed_in": true
```

`codex doctor` 可以检查 Codex 本身的安装情况。broker 的私有日志是状态文件夹中的 `broker.log`：macOS/Linux 为 `~/.local/state/codex-mcp/`，Windows 为 `%LOCALAPPDATA%\CodexMCP\`。

## 先运行 doctor

```bash
python3 server.py doctor
```

`doctor` 会检查安装情况。报告的第一行是总体结论，接着列出问题和警告，每一项都说明该怎么做，然后是通过的检查，最后是它找到的路径和设置。它不使用模型。默认情况下它不启动任何东西，也不修改任何东西：只会创建一个私有的临时文件夹，用完即删。

它会报告：

- 服务器、Python 和 Codex 的版本，以及你的 Codex 版本是否经过测试；
- 实际使用的是哪个 Codex 程序，以及它是怎样找到的；
- 配置文件和状态文件夹，以及状态文件夹是否私有；
- 当前环境中本地套接字（Windows 上是命名管道）能否使用；
- 是否有 broker 在运行，以及它是否由不同的版本或不同的设置启动；
- 状态数据库：版本、大小、一次快速的完整性检查、各数据表是否与其版本相符，以及旁边找到的备份文件。

| 选项 | 额外作用 |
|---|---|
| `--json` | 输出机器可读的结果 |
| `--start-broker` | 允许 `doctor` 启动 broker 和 Codex，这样它还能报告 Codex 是否已登录。这会创建状态文件夹 |
| `--schema` | 生成 Codex 的协议描述，并用它检查服务器发送的消息。不使用模型 |

一切正常时退出码为 0，有警告时为 1，有错误时为 2。如果你使用配置文件，请像 MCP 客户端配置里那样传入：`python3 server.py --config /path/config.toml doctor`。

请在与 MCP 客户端相同的环境中运行 `doctor`。在终端里通过的检查，在受限的容器里可能失败，反过来也一样。

## 常见问题

| 现象 | 原因与解决方法 |
|---|---|
| `Codex executable not found` | MCP 客户端看到的 `PATH` 中没有 Codex。在[配置文件](configuration.zh-CN.md)中把 `codex_binary` 设为绝对路径。如果错误信息以 `Empty or relative PATH entries were ignored` 结尾，说明你的 `PATH` 中有 `.` 这样的条目，这类条目不会被查找。 |
| `codex_binary=... is a relative path` | 在 `codex_binary` 中写 Codex 程序的绝对路径。见[如何查找 Codex](configuration.zh-CN.md#如何查找-codex)。 |
| `Codex 0.167.0 or newer is required` | 运行 `codex update`，然后运行 `python3 server.py stop`。 |
| `optional_native_capability_unavailable: ...` | 已安装的 Codex 缺少这个工具需要的某个可选功能。请改用其他控制方式，或者安装经过测试的 Codex 版本，然后运行 `python3 server.py stop`。 |
| `Codex event ingress budget exceeded` | Codex 产生事件的速度超过了保存速度，为了不超出内存限制，与 Codex 的连接被重置。请调用 `codex_resume`，然后查看 `codex_history`。 |
| `Task has a native protocol error` | Codex 发来了当前版本无法理解的消息。请调用 `codex_resume` 并检查 `codex_history`。如果再次出现，你的 Codex 很可能比测试过的版本更新：请查看 `server.py status` 输出中的 `compatibility`。 |
| `Native thread scope is unverified; resume before ...` | 自上次重启或重新连接以来，这个会话还没有与 Codex 核对过。请先对该任务调用 `codex_resume`，再重复这个操作。在此期间，待处理的审批会保持待处理状态，你仍然可以拒绝它。 |
| `Codex connection changed ...; resume before retrying` | 操作发送途中 Codex 重启了。请调用 `codex_resume`，查看 `codex_get`，再决定是否重复该操作。 |
| `Drive-relative approval paths are not supported` | 补丁或权限请求使用了 `C:folder\file` 这样的 Windows 路径。请拒绝它；Codex 应该使用项目内的路径。 |
| `/mcp` 或客户端的服务器列表中没有 `codex`，或显示失败 | 在终端中运行客户端配置里的那条命令（把 `serve` 换成 `status`），查看错误信息。确认 `server.py` 的路径是绝对路径。 |
| `A broker started by a different version or configuration is still running` | 你更新了代码或修改了设置。运行 `python3 server.py stop`（如有 `--config` 请加上相同参数），然后重试。 |
| `Broker failed to start` / `did not become ready` | 查看状态文件夹中的 `broker.log`。某些受限容器禁止使用 Unix 套接字。 |
| `Work directory ... missing` / `must be an absolute path` | 传入绝对路径，或让主代理用 `codex_projects`（`action: "create"`）创建文件夹。 |
| `Select a dedicated project directory, not the home directory` | 请选择一个项目子文件夹。整个主目录或盘符根目录不能作为项目。 |
| `Project directory overlaps private state` | 项目包含状态文件夹或配置文件，或位于它们之中。请换一个文件夹。 |
| `State database schema version ... is newer than supported version ...` | 这个状态文件夹上次是由更新版本的 codex-mcp 使用的。请把代码更新到那个版本，或者把 `state_dir` 指向另一个文件夹。数据库没有被修改。 |
| `State database migration failed; previous state was preserved` | 升级已被撤销。请保留状态文件夹以及其中的 `state.sqlite3.pre-v*.bak` 备份，并报告这个问题。 |
| `Another task is active in this project` | 另一个任务仍占用着这个文件夹。错误信息在 `Blocking workspace claims:` 之后指出是哪个任务以及原因，例如 `[goal=active]` 或 `[background=active]`。请暂停或清除目标、清空队列或停止后台终端，然后对该任务调用 `codex_resume`。一旦 Codex 确认项目里没有东西会再运行，项目会自动释放，见[工具参考](tools.md#why-a-project-stays-busy)（英文）。 |
| `Commands started before the restart may still be running in this project` | 后台服务停止或崩溃时仍有工作在运行，而 Codex 已经看不到这些命令。请按[仍在运行的命令](#仍在运行的命令)处理，然后对提示中的任务调用 `codex_resume`。 |
| `N Codex sandbox processes are still running in this project (PID ...)` | 仅 Linux。Codex 已不再列出的命令仍在这个文件夹中运行。用 `kill PID` 停止它们，之后项目会自动释放，无需再次调用。如果提示以 "No task accounts for them" 结尾，说明这些进程是在本服务器之外启动的，例如通过 Codex CLI；等待它们结束或停止它们，然后换一个新的 `request_id` 重试。见[工具参考中的 Leftover processes on Linux](tools.md#leftover-processes-on-linux)（英文）。 |
| `This conversation is archived. For archived task ...` | 一个已归档的任务仍占用着项目。按提示操作：用 `codex_archive` 取消归档后调用 `codex_resume`，或用 `codex_delete` 删除它。 |
| `Task-family reconciliation did not settle within 5 seconds` | `codex_delete` 等待服务器对该任务组的自身检查（例如刚启动后），但这些检查没有及时完成。没有删除任何内容。先查看 `codex_get`，再换一个新的 `request_id` 重试。 |
| `send and review nodes must name an earlier start node as target` | 在[计划](tools.md#plans)（英文）中，`send` 或 `review` 步骤使用前面某个 `start` 步骤的对话：写成 `"target": {"node": "ID"}`，并把那个步骤列在 `after` 中。 |
| `A plan stores its prompts until they run; remove the value that looks like a password, token or key ...` | 从提示中指出的步骤里删掉这个机密值。计划会把提示保存在状态数据库中，直到该步骤运行。 |
| `This plan was removed by cleanup and cannot run again under the same identity` | 修改计划的 `name` 后重新提交，它会作为新计划运行。 |
| 计划步骤一直是 `uncertain` | 它的结果未知，例如重启之后。对 `inspect` 指出的任务调用 `codex_resume`，之后计划会继续。如果步骤仍然是 `uncertain`，说明服务器无法确定哪一轮属于这个步骤；见[结束一直受阻的计划](tools.md#plans)。 |
| `Stored dependency plan operation identity is inconsistent; inspect state before running more work` | 计划步骤的存储记录与步骤不一致，服务器不会再为它启动任何工作。运行 `doctor`，用 `inspect` 查看计划、用 `codex_history` 查看任务，然后取消计划。 |
| `This task never reached Codex; pass confirm_thread_id equal to the task id to delete it` | 这个任务没有 Codex 对话。把任务 ID 作为 `confirm_thread_id` 传入。见[从未到达 Codex 的任务](tools.md#a-task-that-never-reached-codex)。 |
| `No Codex thread was created for this task` | 任务的启动仍在进行，或者记录无法证明没有运行任何东西。等启动结束后用 `codex_get` 检查任务，再调用 `codex_resume`。如果消息仍然出现，运行 `doctor` 并按[报告问题](#报告问题)一节导出事件结构。 |
| `Native turn identity is incomplete; workspace ownership was retained` 以及其他以 `workspace ownership was retained` 结尾的消息 | 重启之后，服务器无法从 Codex 的历史或自己的记录证明任务的最后一个回合已经结束，所以项目仍被占用。先查看 `codex_history`，再调用 `codex_resume`。 |
| `Inline review turn identity is unresolved; use codex_resume, then codex_history` 或 `Inline review turn identity is ambiguous across threads; ...` | 服务器无法证明哪一个回合是内联代码审查，通常是子代理内部的审查。为了不做猜测，任务保持忙碌状态。调用 `codex_resume`，然后查看 `codex_history`。 |
| `Revert requires a root turn id; use logical_turn_id from codex_history` | 这个 ID 属于另一个回合内部的回合，例如内联代码审查。请使用该条目的 `logical_turn_id`。 |
| `Turn was not found in bounded native history; inspect codex_history before reverting` | 服务器查看了最近 500 个回合，没有找到这一回合。在 `codex_history` 中核对 ID。 |
| `Git changed during version verification; retry after the installation finishes.` | 服务器检查 Git 版本时，Git 程序被替换了，通常是因为 Git 正在更新。等更新完成后再重试。 |
| `Family budget ... reached: observed ..., limit ...` | 任务组的[预算](tools.md#family-usage-and-budgets)（英文）已用完，或软限制让它停了下来。在根任务上用 `codex_settings`（`scope: "next"`，`budget`）提高或去掉限制，然后换一个新的 `request_id` 重试。 |
| `Cooperative budget stop; may overshoot` | 超过了某个软预算限制，正在运行的回合被中断。查看 `codex_get` 中的 `budget_stop` 和 `family_usage`。已经执行的操作不会被撤销。 |
| `expected_turn_id requires an active matching turn` | 传给 `codex_delegate` 的回合已经结束。父任务空闲时不要传 `expected_turn_id`。 |
| `Managed worktree actions require worktrees = true` | 在设置文件中设置 `worktrees = true`，然后重启 broker。 |
| `Worktree changes need the main checkout to be idle` | 有任务正在仓库的主检出目录中工作。等它结束后，换一个新的 `request_id` 重试。见[工具参考中的 Worktrees](tools.md#worktrees)（英文）。 |
| `... prepare this worktree manually instead` | 该仓库使用了可能让服务器的 Git 运行程序或从别处读取数据的功能（例如 Git LFS 等过滤器、包含的配置、稀疏检出等）。请自己用 `git worktree add` 创建 worktree，再把它选为项目。 |
| `The worktree has unsaved or ignored files ...; nothing was removed` | 先保存或移走 worktree 中的改动。可以用 `handoff` 把它们保存为补丁。 |
| `The worktree has commits outside its base; use keep_branch=True ...` | 使用 `keep_branch: true` 清理，会删除文件夹并保留带有这些提交的分支。 |
| `The process list could not be read; workspace ownership is kept` | 仅 Linux。服务器无法读取 `/proc`，因此无法判断是否仍有残留进程，于是保持项目占用。运行 `python3 server.py doctor` 查看原因。如果无法让 `/proc` 可读，请设置 `sandbox_process_check = false` 并重启 broker。 |
| `Workspace changed during the process check` | 服务器查找残留进程期间，项目的占用情况连续三次发生变化。先查看 `codex_get`，再换一个新的 `request_id` 重试。 |
| `This conversation no longer exists in Codex or cannot be read` | 这个对话已从 Codex 中移除，例如它的会话文件被删除了。使用 `codex_delete` 并填入提示中的 `confirm_thread_id` 来释放项目。任务的审计记录会保留。见[工具参考中的 A task whose conversation is gone](tools.md#a-task-whose-conversation-is-gone)（英文）。 |
| `File approval proposal is stale or unknown` | 补丁在显示之后被 Codex 修改了。请重新审核该请求。 |
| `Another task is active in this project` | 同一文件夹中同一时间只能有一个独立任务。等待、中断它，或使用其他文件夹 / worktree。 |
| `Concurrent task limit reached` | 结束或中断一个任务，或者调大 `max_tasks`。 |
| `Configuration must be owned by this user...` | 对配置文件执行 `chmod 600`（macOS/Linux）。在 Windows 上，确认只有你的账户可以修改它。 |
| `State directory must be owned by this user with mode 0700` | 对状态文件夹执行 `chmod 700`，或选择新的 `state_dir`。 |
| `state_dir is too long for a Unix socket path` | 选择更短的 `state_dir`。 |
| `The legacy unelevated Windows sandbox cannot enforce denied reads` | 把 Codex 切换回默认的 Windows 沙箱（见[安装细节](installation.zh-CN.md#windows-说明)）。 |
| Codex 内部提示某个工具"not found"（例如装在特殊位置的编译器） | 把它所在的文件夹加入 `toolchain_paths`，然后 `stop` broker。 |
| `Model is not in the visible Codex catalog` / `Unsupported reasoning effort` | 调用 `codex_models`，使用其中列出的组合。 |
| `Previous operation with this request_id is uncertain` | 这个键已经用过，且那次操作失败或被打断。检查任务后，换一个新的 `request_id`。 |
| `Resume could not commit a consistent native recovery snapshot` | `codex_resume` 从 Codex 读取任务状态时，任务连续三次发生了变化。没有确认任何内容，也没有释放任何占用。先查看 `codex_get`，再换一个新的 `request_id` 重新调用 `codex_resume`。 |
| 原因已经消除，却仍收到同样的拒绝 | 在任何内容到达 Codex 之前就被拒绝的调用，会连同它的 `request_id` 一起保存，同一个键会返回同样的错误。请换一个新的 `request_id` 重新调用。 |
| `request_id was already used with different arguments` | 每个新操作都要使用新的、唯一的 `request_id`。 |
| `Operation key has expired` | 那次调用保存的结果已被 `prune` 删除。这个键会有意保持锁定。检查任务后，为新的工作换一个新的 `request_id`。 |
| `Event cursor has expired` | 游标所指的事件已被 `prune` 删除。从错误中的 `resume_after` 继续。 |
| `Pruning requires a stopped broker` | 运行 `python3 server.py stop`（使用相同的 `--config`），然后再次运行 `prune`。 |
| 状态目录变得很大 | 见[备份与清理](installation.zh-CN.md#备份与清理)。不会自动删除任何内容。 |

## 任务处于等待状态

`waiting` 状态的任务需要一个回答。

1. 用 `codex_requests` 列出待处理请求（或读取 `codex_get` 中的 `pending_requests`）。
2. 对于 `parent_question`，主代理用 `codex_respond` 和 `{"text": "..."}` 回答。
3. 对于审批和用户问题，使用 `codex_review_request`。如果客户端无法显示提示框，在终端中运行 `python3 server.py review REQUEST_ID`。
4. Codex 重启后请求会过期。如果回答因过期被拒绝，请调用 `codex_resume`，让 Codex 重新发起请求。

如果取消子代理的审批后父任务一直在等待，请中断父任务的这一轮，等待 `interrupted`，再发送一条说明下一步做法的后续指令。

## 关闭客户端后任务就停止了

有些 MCP 宿主程序退出时会结束所有子进程，broker 也会被结束。请在打开客户端之前，先在普通终端中运行 `python3 server.py start` 启动 broker。

## 崩溃或重启后的恢复

- 正在进行的任务会变为 `needs_resume`。调用 `codex_resume`，阅读 `codex_history`，再发送后续指令。不会自动重放任何内容。
- 从上次保存的游标继续读取 `codex_events`，已提交的事件不会丢失。
- `uncertain` 状态表示某个操作的确认丢失了。重复任何操作之前，先查看 `codex_get` 和 `codex_history`。
- 如果排队的消息提示"is no longer pending"，说明 Codex 很可能已经开始执行它。请检查任务，不要重复添加。
- 重启前已经结束的任务不需要任何操作，它的项目会在几秒内重新可用。重启时仍在工作的任务会一直占用项目，直到你对它调用 `codex_resume`。请先阅读下一节。

## 仍在运行的命令

服务器只能通过 Codex 的报告了解有哪些命令在运行。以下三种情况会让命令继续在项目中运行，而没有任何任务显示它：

- 后台服务停止或崩溃时，某个回合或后台终端仍在运行。停止后台服务并不会停止这些命令。
- 命令自己脱离了 Codex，例如使用 `nohup ... &`。
- 在 Linux 上使用 Codex 0.167.0 时，Codex 报告已终止的后台终端可能仍在运行。这是用 `codex_terminals` 的 `terminate` 和 `clean` 实测到的，属于 Codex 的问题，不是本服务器的问题。

**在 Linux 上，服务器会自己查找这类残留进程。**只要找到，它就保持项目被占用，在拒绝信息和 `codex_get` 中列出它们的 PID，并在它们消失后释放项目。`python3 server.py doctor` 也会列出它们。在 macOS 和 Windows 上，以及对于已经脱离 Codex 沙箱的进程，你需要自己查找。

查找并停止残留进程的方法：

```bash
pgrep -af codex-linux-sandbox        # Linux：Codex 启动的沙箱命令，命令行中带有项目文件夹
ps -eo pid,etime,args | grep /path/to/project
kill PID
```

macOS 上同样使用 `ps`。Windows 上使用任务管理器或 `Get-Process`。在 Linux 上，要停止的是命令行中带有你的项目路径的 `codex-linux-sandbox` 进程，它里面的命令会随之结束。如果服务器给出了重启警告，再对该任务调用 `codex_resume`。对于 Linux 检查发现的残留进程，不需要任何调用。

为避免这种情况，运行 `server.py stop` 之前，先用 `codex_list` 查看是否有正在运行的任务，用 `codex_terminals` 查看是否有后台终端。

## 报告问题

请附上 `python3 server.py --version`、`codex --version` 的输出、操作系统、失败的工具调用以及（已脱敏的）错误信息，并说明离线测试（`python3 -m unittest discover`）是否通过。不要上传状态文件夹、`~/.codex` 或完整的对话历史，其中可能包含私有代码和提示词。安全问题请参见 [SECURITY.md](../SECURITY.md)。

### 只分享经过，不分享内容

如果问题与任务的行为有关（任务卡住、回答丢失、状态不对），可以只附上任务的*结构*，不包含其内容：

```bash
python3 server.py incident-export --task-id TASK_ID
```

这只会输出预览，不写入任何文件。预览中是该任务的事件和请求序列。所有文本、路径、命令和输出都被替换为占位符，例如 `{"$omitted": {"type": "string", "length": 412}}`；所有 ID 都被替换为中性的引用，例如 `ref-0003`。请先读一遍。如果愿意分享，带上它输出的 `preview_id` 再运行一次：

```bash
python3 server.py incident-export --task-id TASK_ID --output incident.json --confirm-preview PREVIEW_ID
```

只有当数据与预览时完全相同，文件才会被写入。这个命令不会启动 broker 或 Codex，不会向任何地方发送数据，也绝不会覆盖已有文件。它也不会升级数据库：刚更新之后，请先启动一次服务器（`python3 server.py status`），再导出。

| 选项 | 含义 |
|---|---|
| `--task-id` | 任务 ID，最多可以给出 8 次 |
| `--after`、`--limit` | 包含哪些事件：某个游标之后的事件（默认 0），默认 200 条，最多 1,000 条 |
| `--json` | 输出 JSON |
| `--output`、`--confirm-preview` | 看过预览之后写入文件 |

维护者可以用模拟的 Codex 重放这样的文件，并把它变成测试，见[开发文档](development.md#incident-fixtures)。
