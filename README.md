# cc-cursor-bridge

使用说明见 [docs/USAGE.md](docs/USAGE.md)。

Claude Code 插件加 Cursor skill。Cursor agent 在这台 Mac 上调用本机 `claude -p` 写代码时，插件负责三件事：把每个 hook 事件按顺序记下来，拦住危险的 Bash 和文件操作，并按事件模式决定是等待回复、异步推送，还是只写日志。33 个事件都还注册着，默认分流写在 `config/bridge.json` 的 `events`。

本机 Claude Code 是 **2.1.306**（`/Users/timon/.local/bin/claude`）。事件清单以官方 hooks 文档和这个版本二进制里的合法事件集合为准，两者一致。

## 目录

```text
.claude-plugin/plugin.json          Claude Code 插件清单，hooks 指向 claude/hooks/
.claude-plugin/marketplace.json     本地或 GitHub marketplace
.cursor-plugin/plugin.json          Cursor 插件清单，skills 指向 cursor/skills/
.cursor-plugin/marketplace.json     从 GitHub 导入 Cursor marketplace 时用
claude/hooks/hooks.json             33 个 hook，全部进 scripts/dispatch.py
scripts/dispatch.py                 hook 入口
scripts/bridge.py                   日志、规则、等待、notifier 调用点
scripts/run_claude_bridged.sh       claude -p 启动；prompt 走 stdin，避开 --allowedTools
scripts/test_bridge.py              不联网的单元测试
config/rules.json                   危险操作规则，可关、可加
config/bridge.json                  notifier 与等待通道，默认都关
cursor/skills/claude-code-bridge/   Cursor skill 的唯一副本
cursor/hooks.json                   空的 Cursor hooks，避免误读 CC 的 hooks
docs/USAGE.md                       给使用者和 agent 的简短说明
docs/shadow-diff.md                 AHP shadow diff：CC 适配层与候选 producer 逐事件对比
tools/shadow_diff/                  shadow diff 工具（python3 -m tools.shadow_diff），不属于插件
examples/sandbox/                   claude --plugin-dir 自测目录，产物不入库
```

仓库根下没有 `skills/`，也没有 `hooks/hooks.json`。

## 为什么 skill 不放在根目录 `skills/`

两边都会扫默认目录，放在一起会互相误加载：

- Claude Code 插件自动扫描插件根的 `skills/`，并把里面的 `SKILL.md` 放进 Claude 的上下文。这份 skill 是教 Cursor agent 怎么启动 `claude` 的，Claude 自己读到会去套一层「再启动一个 claude」。
- Cursor 插件在没写 `skills` 字段时自动扫描插件根的 `skills/`。写了 `skills` 之后，默认目录不再扫描。
- Cursor 插件在没写 `hooks` 字段时会解析插件根的 `hooks/hooks.json`。那是 Claude Code 的事件名（`PreToolUse` 这种），和 Cursor 自己的 hook（`preToolUse`、`beforeShellExecution`）不是一套。

所以 Claude Code 的 hook 放在 `claude/hooks/hooks.json`，由 `.claude-plugin/plugin.json` 的 `hooks` 指过去。Cursor 的 skill 放在 `cursor/skills/claude-code-bridge/`，由 `.cursor-plugin/plugin.json` 的 `skills` 指过去，同时把 `hooks` 指到空的 `cursor/hooks.json`。

Cursor 还会从 `.claude/skills/` 加载 skill。这个仓库没有 `.claude/skills/`，也没有项目级 `.cursor/skills/`。Claude Code 2.1.306 里出现 `.cursor/skills` 的那段代码是迁移扫描（把 Cursor 配置导入成 Claude 配置），不是每次会话自动加载。Skill 只改 `cursor/skills/claude-code-bridge/SKILL.md`。

## Claude Code 插件

### 安装

开发时不要全局安装，用会话参数即可：

```bash
claude --plugin-dir /path/to/cc-cursor-bridge
```

带 `-p` 跑任务时，prompt 从 stdin 送入，用 `scripts/run_claude_bridged.sh --prompt-file prompt.txt`（或 `claude -p --plugin-dir /path/to/cc-cursor-bridge … < prompt.txt`）。不要把任务说明放在 `--allowedTools` 后面：那个 flag 会吃掉后续参数，Claude 的 stderr 是 `Input must be provided either through stdin or as a prompt argument`。见 [docs/USAGE.md](docs/USAGE.md)。

本地 marketplace（会写入 Claude Code 的 marketplace 配置，自测没有跑这条）：

```bash
claude plugin marketplace add /path/to/cc-cursor-bridge
claude plugin install cc-cursor-bridge@cc-cursor-bridge
```

GitHub（私有仓库，需要当前 `gh` / git 身份能读 `ic-timon/cc-cursor-bridge`）：

```bash
claude plugin marketplace add ic-timon/cc-cursor-bridge
```

会话里的安装行：

```text
/plugin install cc-cursor-bridge --marketplace ic-timon/cc-cursor-bridge
```

`claude plugin validate` 对本目录的结论是通过。它另外提示：没有图标、没有 LICENSE，所以还不能提交到 Anthropic 的 plugin directory。这不是本次目标。

### 配置

| 项 | 作用 | 默认 |
| --- | --- | --- |
| `CC_BRIDGE_DIR` | 日志根目录 | `~/.cc-bridge` |
| `CC_TASK_ID` | 任务目录名。非法字符（含 `/`、`..`）会丢掉，改用 `session_id` | 无，则用 `session_id` |
| `CC_BRIDGE_RULES` | 另指一份规则 JSON | `config/rules.json` |
| `CC_BRIDGE_CONFIG` | 另指一份 bridge JSON | `config/bridge.json` |
| `CC_BRIDGE_NOTIFIER` | `1` 打开 notifier | 关 |
| `CC_BRIDGE_CURSOR_AGENT_ID` | 将来 followup 的 agent id | 空 |
| `CC_BRIDGE_CURSOR_API_KEY` | 只从环境读取，不入库、不进日志 | 空 |
| `CC_BRIDGE_WAIT` | `1` 打开等待通道 | 关 |
| `CC_BRIDGE_WAIT_PRE_TOOL` | `1` 时 PreToolUse 也等待 | 关 |
| `CC_BRIDGE_WAIT_TIMEOUT` | 等待秒数 | 120 |
| `CC_BRIDGE_AHP_DIR` | AHP v1 目录根（0.2.0） | `$CC_BRIDGE_DIR/ahp` |
| `CC_BRIDGE_AHP` | `0` 时不写 AHP 目录 | `1` |
| `CC_BRIDGE_LEGACY` | `0` 时不写旧格式目录，CLI 默认走 AHP | `1` |
| `CC_BRIDGE_FAULT` | 仅测试：`crash` 让 hook 在处理中抛异常，用来验证 fail-closed | 空 |

规则文件坏了会回退到脚本里的同一套内置规则，并在日志 `summary.config_note` 里写原因。把某条规则的 `enabled` 设为 `false` 就关掉它。还可以加 `command_regex` 或 `path_regex`。

### 事件日志

路径：`$CC_BRIDGE_DIR/tasks/<CC_TASK_ID 或 session_id>/events.jsonl`

每行一个 JSON：`seq`、本地 `ts`、`event`、`session_id`、`summary`。摘要里有工具名、路径、截断后的命令、Edit 的 old/new、Bash 输出。默认单字段 240 字。写入用文件锁，`seq` 是写完的顺序。异步 hook 可能比后触发的同步 hook 更晚落盘。

日志里如果出现 `CC_BRIDGE_CURSOR_API_KEY`、`ANTHROPIC_API_KEY`、`ANTHROPIC_AUTH_TOKEN` 的值，会换成 `[redacted]`。

### 危险操作

只在 `PreToolUse` 上拒绝，用 `hookSpecificOutput.permissionDecision = deny`。覆盖 Bash、PowerShell，以及 Read、Edit、Write、MultiEdit、NotebookEdit、Glob、Grep。

| 规则 id | 行为 |
| --- | --- |
| `bash-rm-rf` | `rm` 同时带递归（`-r`/`-R`/`--recursive`）和强制（`-f`/`--force`）；PowerShell `Remove-Item -Recurse -Force` |
| `bash-git-push-force` | `git push` 的 `--force`、短选项里的 `f`、`--force-with-lease` |
| `secret-env-file` | 路径或命令里的 `.env`、`.env.*`。放行 `.env.example`、`.env.sample`、`.env.template` |
| `ssh-dir` | `~/.ssh`、`$HOME/.ssh`，以及解析后落在用户主目录 `.ssh` 下的路径 |
| `agent-artifact-dirs` | 路径或命令里的 `.claude-flow/`、`.swarm/`（agent 运行产物；PR 前不要 `git add -A`） |

Grep 的搜索 pattern 不参与判断，只看 `path` 和 `glob`，避免搜源码里的字符串 `.env` 被误拦。项目里另有一个不是用户主目录的 `.ssh` 目录时不拦。`rm -r`（没有 `-f`）和 `git push origin +branch` 这种 refspec 不拦。

### Notifier

`config/bridge.json` 的 `notifier.enabled` 默认 `false`，只写本地日志。打开之后仍然不发 HTTP。调用点在 `scripts/bridge.py` 的 `maybe_notify`：把打算发给 Cursor Cloud Agents API 的 followup 写到任务目录的 `outbox/`。计划中的请求是 `POST https://api.cursor.com/v0/agents/{agent_id}/followup`，Bearer 用 `CC_BRIDGE_CURSOR_API_KEY`。outbox 里只有 `api_key_configured: true/false`，没有密钥本身。

默认会排队的事件：`PermissionRequest`、被拒绝的 `PreToolUse`（`pre_tool_use: denied_only`）、`PostToolUseFailure`、`StopFailure`、`Stop`、`Notification`。notifier 关着时这些事件的日志字段 `notify` 是 `disabled`。

### 事件模式

默认按 `config/bridge.json` 的 `events` 分流。单次可用 `CC_BRIDGE_EVENT_MODE_<事件名>=wait|push|log|off` 覆盖。`CC_BRIDGE_WAIT_ALL=1` 仍可强制 33 个事件都走 pending，用来调试；PUSH/LOG 的 hook 是异步的，Claude Code 不会为它们阻塞。

| 模式 | 事件 | 行为 |
| --- | --- | --- |
| wait | PreToolUse、PermissionRequest、Elicitation、Stop | 同步，timeout 180。写 `pending/<seq>-<事件>.json` 并等到 `replies/<seq>.json` |
| push | SessionStart、UserPromptSubmit、UserPromptExpansion、PostToolUse、PostToolUseFailure、StopFailure、SubagentStop、TaskCreated、TaskCompleted、TeammateIdle、WorktreeCreate、PreCompact、PreModelSwitch | 写 `push/<seq>-<事件>.json` 并追加 `push/stream.jsonl`，不阻塞 |
| log | Setup、InstructionsLoaded、MessageDisplay、PermissionDenied、PostToolBatch、Notification、SubagentStart、ConfigChange、CwdChanged、DirectoryAdded、FileChanged、WorktreeRemove、PostCompact、PostModelSwitch、ElicitationResult、SessionEnd | 只追加 `events.jsonl` |
| off | 无默认项 | 尽快返回。PreToolUse 命中规则时仍然 deny 并记日志 |

SessionStart 和 WorktreeCreate 在 `hooks.json` 里保持同步。SessionStart 不等回复，这样 `CC_BRIDGE_WATCH` 能注入 `watchPaths`；设了这个变量时，即使模式被改成 wait 也不等人。WorktreeCreate 自己执行 `git worktree add`，把路径打到 stdout，同时 push 一条通知。

等待失败策略：PreToolUse 和 PermissionRequest 超时 deny，Elicitation 超时 decline，Stop 超时放行结束。Stop 的回复 `{"decision":"block","reason":"..."}` 会让 Claude Code 继续；`{"ack":true}` 结束。`stop_hook_active` 为 true 且没有明确 block 时放行。每个任务最多阻断 `stop.max_blocks` 次（默认 3）。外部依赖这次解不了时 ack 并把缺口写进 PR/报告，不要抬高 `CC_BRIDGE_STOP_MAX_BLOCKS` 空转。

助手命令：`python3 scripts/bridge.py pending <task>`、`reply <task> <seq> <json>`、`push <task> [--since <seq>]`、`watch <task>`。

旧的窄等待 `CC_BRIDGE_WAIT=1` 只在事件模式为 `log` 时再生效；0.2.0 起它在 AHP 目录里也是一个 gate（pending + ack），旧目录仍写 `pending/<tool_use_id>.json`。

### 事件清单

PUSH/LOG 的 hook 是 `async: true`（timeout 30，SessionEnd 55，因为官方 SessionEnd 预算最高 60 秒）。WAIT 以及 SessionStart、WorktreeCreate 是同步，timeout 180。UserPromptSubmit / PreModelSwitch / PostModelSwitch 不写 timeout 时默认只有 30 秒，MessageDisplay 只有 10 秒，所以异步事件也显式写了 timeout。

| 事件 | 模式 | 现在做什么 |
| --- | --- | --- |
| SessionStart | push，同步不等人 | 记日志并 push。`CC_BRIDGE_WATCH` 时注入 watchPaths |
| Setup | log | 记日志 |
| InstructionsLoaded | log | 记日志 |
| UserPromptSubmit | push | push，不阻塞 |
| UserPromptExpansion | push | push，不阻塞 |
| MessageDisplay | log | 记日志，不改显示文本 |
| PreToolUse | wait | 命中规则则立刻 deny 且不写 pending；否则等回复 |
| PermissionRequest | wait | 等回复 |
| PermissionDenied | log | 记日志 |
| PostToolUse | push | push，含截断后的工具输出 |
| PostToolUseFailure | push | push；notifier 打开时进 outbox |
| PostToolBatch | log | 记日志 |
| Notification | log | 记日志；notifier 打开时进 outbox |
| SubagentStart | log | 记日志 |
| SubagentStop | push | push |
| TaskCreated / TaskCompleted | push | push |
| Stop | wait | 等验收回复。block 继续，ack 结束。notifier 打开时进 outbox |
| StopFailure | push | push；notifier 打开时进 outbox |
| TeammateIdle | push | push |
| ConfigChange | log | 记日志 |
| CwdChanged | log | 记日志 |
| DirectoryAdded | log | 记日志 |
| FileChanged | log | 记日志。没有预置文件名；SessionStart 注入 `watchPaths` 之后才会响 |
| WorktreeCreate | push，同步 | 自己执行 `git worktree add` 并返回路径，同时 push |
| WorktreeRemove | log | 记日志，并 `git worktree remove --force`。只接受 `.claude/worktrees/` 下的路径 |
| PreCompact | push | push，默认不阻断 |
| PostCompact | log | 记日志 |
| PreModelSwitch | push | push |
| PostModelSwitch | log | 记日志 |
| Elicitation | wait | 等 accept 或 decline。超时 decline |
| ElicitationResult | log | 记日志 |
| SessionEnd | log | 异步记日志。timeout 55 秒 |

## 本机不支持的 hook 事件

没有。2.1.306 二进制里的合法事件数组和官方文档相同，就是上面这 33 个。`StatusLine` 和 `FileSuggestion` 出现在另一组名字里，它们不是 hook 事件，所以没有注册。

`claude plugin validate` 没有报告 unknown hook event。自测加载这个插件时，debug 日志里也没有 unknown hook event。

## Cursor 插件

`.cursor-plugin/plugin.json` 和 `.claude-plugin/` 都在仓库根上。Cursor 侧只有一个 skill：`claude-code-bridge`。内容是怎么生成 `CC_TASK_ID`、怎么用 `scripts/run_claude_bridged.sh`（`claude -p`，prompt 走 stdin，`--output-format stream-json --verbose`）启动、推荐的权限参数（不用 bypass）、事件文件在哪、怎么读、怎么写 pending 回复、怎么判断结束、出错怎么办。

装到本机 Cursor IDE（复制，不用 symlink；指向仓库外部的 symlink 会被跳过）：

```bash
rsync -a --delete \
  --exclude .git \
  --exclude .cursor \
  --exclude examples/sandbox \
  --exclude __pycache__ \
  /path/to/cc-cursor-bridge/ \
  ~/.cursor/plugins/local/cc-cursor-bridge/
```

排除 `.cursor/`，避免本地插件目录里再带一份项目级 skill。然后在 Cursor 里 Reload Window，到 Customize 里确认 skill。这台 self-hosted worker 的实测没有加载 `~/.cursor/plugins/local`。

给 cloud agent 用的副本装到个人 skill 目录，不要装进 `~/.agents/skills/` 或 `~/.claude/skills/`：

```bash
scripts/install_cursor_skill.sh
scripts/install_cursor_skill.sh --uninstall
```

默认目标是 `$HOME/.cursor/skills/claude-code-bridge/`，只替换这一个目录。装完要在 Cursor 里打开 Customize → Skills，执行 “Sync Skills for Cloud Agents”。同步会把这个目录从 `~/.cursor/skills/` 挪进 AgentStores，所以之后改了 skill 要再跑脚本并再同步一次。本机同步后的路径是 `~/Library/Application Support/Cursor/AgentStores/cursor_agent_stores/u299329845/files/skills/claude-code-bridge/`。现在不要对这台机器再跑一遍默认安装，以免和已经同步走的副本冲突。

本地 IDE 可以另外装到某个工作区，self-hosted cloud agent 不会读它：

```bash
scripts/install_cursor_skill.sh --workspace /path/to/project
scripts/install_cursor_skill.sh --workspace /path/to/project --uninstall
```

`--workspace` 拒绝 `/`、`$HOME` 和不存在的路径。

## 第 2 部分：worker 上实际读到了哪里

文档曾写个人 skill 不会进 self-hosted worker，项目级 `.cursor/skills/` 会。这台机器上的新会话结果相反：cloud agent 只通过 “Sync Skills for Cloud Agents” 加载 skill。

没有被加载的位置：

- 项目或工作区里的 `.cursor/skills/`（仓库里不再放这份副本）
- 工作区根 `/Users/timon/Sync/Test/.cursor/skills/claude-code-bridge/`（当时的临时安装，已删）
- `~/.agents/skills/claude-code-bridge/`（更早一次会话能看到同目录里的 babysit 等 skill；这次核实 cloud agent 并不从这里加载 skill）
- `~/.cursor/plugins/local/cc-cursor-bridge/`

被加载的位置是同步存储：

`~/Library/Application Support/Cursor/AgentStores/cursor_agent_stores/u299329845/files/skills/claude-code-bridge/`

同步之后 `~/.cursor/skills/` 里的这份被移走，该目录变空。上面两个临时副本（工作区根和 `~/.agents/skills`）已经删掉。AgentStores 里的副本和 `~/.cursor/plugins/local` 留着。

### 怎么验证

另起一个 cloud agent，请它回答 skill 列表里 `claude-code-bridge` 的文件路径是不是 AgentStores 下面那份。改 skill 之后的验证顺序是：改 `cursor/skills/claude-code-bridge/SKILL.md`，跑 `scripts/install_cursor_skill.sh`，再在 Customize → Skills 里同步一次，然后开新会话。

## 自测

单元测试不联网：

```bash
python3 scripts/test_bridge.py
/usr/bin/python3 scripts/test_bridge.py
```

两条都应通过（当前 65 个测试，含 `run_claude_bridged.sh --dry-run`）。AHP v1 一致性：

```bash
python3 -m conformance --producer cc-adapter
```

覆盖 `rm -rf`、`git push --force`、`.env` / `.env.example`、`~/.ssh`、日志脱敏、等待超时、notifier 只写 outbox、worktree 往返，以及 `CC_BRIDGE_PROBE` 的探测回复。

33 个 hook 的真实 `claude -p` 跑法在 `tests/hooks/run_all.py`。完整报告写到 `tests/hooks/out/`（不入库）。入库的短摘要是手写的 `tests/hooks/report.md`。探测模式只在设置了 `CC_BRIDGE_PROBE` 时改变 hook 的 stdout，平时行为不变。

带插件的 `claude -p` 自测在 `examples/sandbox/` 里做，不用 `--dangerously-skip-permissions`，也不全局安装。产物被 gitignore。2026-10-21 在 Claude Code 2.1.306 上跑过一次，`CC_TASK_ID=selftest1`，退出码 0，debug 里没有 unknown hook event。插件加载为 `cc-cursor-bridge@inline`，没有把 `claude-code-bridge` 这个 skill 装进 Claude。

`echo hi` 执行成功，PostToolUse 记到 stdout `hi`。`rm -rf scratch` 被 `bash-rm-rf` 拒绝，目录还在。Read `.env` 被 `secret-env-file` 拒绝。debug 里这两次的 stdout 都被解析成 `permissionDecision: deny`，进程退出码是 0。模型看到的工具结果文案是 `PreToolUse hook error:` 加上拒绝原因，那是 Claude Code 把 deny 原因送回模型的说法，不是脚本崩溃。

Write `hello.txt` 没有被插件拒绝（PreToolUse 没有 decision），但这个没有审批界面的 `-p` 会话自己拒绝了它（`asyncAgent` / no approval surface），所以文件没有落盘。`--permission-mode acceptEdits`、`--allowedTools Write` 和临时候选 settings 都没有改变这一点。

日志样例（密钥不在其中）：

```json
{"seq": 6, "ts": "2026-10-21T17:51:31.218+08:00", "event": "PreToolUse", "session_id": "61612314-bc87-42c1-9ff5-a125e29ab8c9", "summary": {"tool_name": "Bash", "command": "echo hi"}}
{"seq": 9, "ts": "2026-10-21T17:51:37.269+08:00", "event": "PreToolUse", "decision": "deny", "rule_id": "bash-rm-rf", "decision_reason": "拦截递归强制删除（rm -rf，或 PowerShell 的 Remove-Item -Recurse -Force）", "summary": {"tool_name": "Bash", "command": "rm -rf scratch"}}
{"seq": 11, "ts": "2026-10-21T17:51:43.957+08:00", "event": "PreToolUse", "decision": "deny", "rule_id": "secret-env-file", "decision_reason": "拦截读写 .env（放行 .env.example、.env.sample、.env.template）", "summary": {"tool_name": "Read", "file_path": ".../examples/sandbox/.env"}}
```

## 已知限制

- 异步事件的 `seq` 是写盘顺序，不是 Claude Code 触发顺序。
- `FileChanged` 没有预置监听列表。
- `WorktreeCreate` 一旦注册就会替换 Claude Code 默认的 git worktree。脚本改为在 `<cwd>/.claude/worktrees/<slug>` 执行 `git worktree add`，分支名 `cc-bridge/<slug>`。失败时 worktree 创建失败。
- `WorktreeRemove` 失败时不执行 `rm -rf`。
- `git push origin +branch` 不拦。只有 `-f` 而没有 `-r` 的 `rm` 不拦。
- 默认只有 PreToolUse、PermissionRequest、Elicitation、Stop 会等回复。PreToolUse 的 allow 会跳过该次权限提示。Stop 最多阻断 3 次。外部依赖解不了就 ack，不要抬 `CC_BRIDGE_STOP_MAX_BLOCKS`。
- notifier 不发网络请求。
- 日志字段会截断。
- hook 依赖 `PATH` 里的 `python3`。脚本按 3.9 写，本机 Homebrew 3.14 和 `/usr/bin/python3` 3.9 都跑过单元测试。

## 刻意没做

- 不调用 Cursor API，仓库里没有任何密钥。
- 不改 `~/.claude/settings.json`，不改 cc-switch（`127.0.0.1:15721`），不全局 `claude plugin install`。
- 不合并 PR。
- 不在这次会话里宣称新 skill 已被 worker 发现。发现要等新会话。
- 不削减 hook。33 个都还在。
- 不提交 `examples/sandbox/` 里的运行产物。
