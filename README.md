# cc-cursor-bridge

Claude Code 插件加 Cursor skill。Cursor agent 在这台 Mac 上调用本机 `claude -p` 写代码时，插件负责三件事：把每个 hook 事件按顺序记下来，拦住危险的 Bash 和文件操作，并留出一条本地通道，让发起任务的 Cursor agent 以后可以批准或拒绝。策略是先把文档里的 hook 全部接上，以后再从规则文件里一条条削掉。

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
scripts/test_bridge.py              不联网的单元测试
config/rules.json                   危险操作规则，可关、可加
config/bridge.json                  notifier 与等待通道，默认都关
cursor/skills/claude-code-bridge/   Cursor 插件里的 skill（规范位置）
cursor/hooks.json                   空的 Cursor hooks，避免误读 CC 的 hooks
.cursor/skills/claude-code-bridge/  项目级 skill，给 self-hosted worker 测试
examples/sandbox/                   claude --plugin-dir 自测目录，产物不入库
```

仓库根下没有 `skills/`，也没有 `hooks/hooks.json`。

## 为什么 skill 不放在根目录 `skills/`

两边都会扫默认目录，放在一起会互相误加载：

- Claude Code 插件自动扫描插件根的 `skills/`，并把里面的 `SKILL.md` 放进 Claude 的上下文。这份 skill 是教 Cursor agent 怎么启动 `claude` 的，Claude 自己读到会去套一层「再启动一个 claude」。
- Cursor 插件在没写 `skills` 字段时自动扫描插件根的 `skills/`。写了 `skills` 之后，默认目录不再扫描。
- Cursor 插件在没写 `hooks` 字段时会解析插件根的 `hooks/hooks.json`。那是 Claude Code 的事件名（`PreToolUse` 这种），和 Cursor 自己的 hook（`preToolUse`、`beforeShellExecution`）不是一套。

所以 Claude Code 的 hook 放在 `claude/hooks/hooks.json`，由 `.claude-plugin/plugin.json` 的 `hooks` 指过去。Cursor 的 skill 放在 `cursor/skills/claude-code-bridge/`，由 `.cursor-plugin/plugin.json` 的 `skills` 指过去，同时把 `hooks` 指到空的 `cursor/hooks.json`。

Cursor 还会从 `.claude/skills/` 加载 skill。这个仓库没有 `.claude/skills/`。Claude Code 2.1.306 里出现 `.cursor/skills` 的那段代码是迁移扫描（把 Cursor 配置导入成 Claude 配置），不是每次会话自动加载。项目级 `.cursor/skills/` 因此不会在普通 `claude -p` 里变成 Claude 的 skill。

`.cursor/skills/claude-code-bridge/SKILL.md` 和插件里那份内容相同，只为了测 self-hosted worker。文档写明个人 skill 不会同步到 worker，仓库里的项目级 skill 会。两份要一起改。

## Claude Code 插件

### 安装

开发时不要全局安装，用会话参数即可：

```bash
claude --plugin-dir /path/to/cc-cursor-bridge
```

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

Grep 的搜索 pattern 不参与判断，只看 `path` 和 `glob`，避免搜源码里的字符串 `.env` 被误拦。项目里另有一个不是用户主目录的 `.ssh` 目录时不拦。`rm -r`（没有 `-f`）和 `git push origin +branch` 这种 refspec 不拦。

### Notifier

`config/bridge.json` 的 `notifier.enabled` 默认 `false`，只写本地日志。打开之后仍然不发 HTTP。调用点在 `scripts/bridge.py` 的 `maybe_notify`：把打算发给 Cursor Cloud Agents API 的 followup 写到任务目录的 `outbox/`。计划中的请求是 `POST https://api.cursor.com/v0/agents/{agent_id}/followup`，Bearer 用 `CC_BRIDGE_CURSOR_API_KEY`。outbox 里只有 `api_key_configured: true/false`，没有密钥本身。

默认会排队的事件：`PermissionRequest`、被拒绝的 `PreToolUse`（`pre_tool_use: denied_only`）、`PostToolUseFailure`、`StopFailure`、`Stop`、`Notification`。notifier 关着时这些事件的日志字段 `notify` 是 `disabled`。

### 等待回复

默认关。打开后：

- `PermissionRequest` 会等（`on_permission_request` 默认 true）
- `PreToolUse` 默认不等；规则已经 deny 的也不会再等
- 请求写到 `pending/<id>.json`，回复写到 `replies/<id>.json`
- 回复形如 `{"decision":"allow","reason":"..."}` 或 `deny`
- 超时默认 deny

`PreToolUse` 上的 `allow` 会让这一次工具调用跳过权限提示。PermissionRequest 的 allow/deny 走官方的 `decision.behavior`。

### 事件清单

同步的是要做决定、要返回 worktree 路径，或处在进程收尾、不能丢的事件。其余 `async: true`，避免拖慢 Claude Code。`-p` 结束时还没写完的异步 hook 可能被取消。

| 事件 | 模式 | 现在做什么 |
| --- | --- | --- |
| SessionStart | 异步 | 记日志 |
| Setup | 异步 | 记日志 |
| InstructionsLoaded | 异步 | 记日志 |
| UserPromptSubmit | 异步 | 记日志 |
| UserPromptExpansion | 异步 | 记日志 |
| MessageDisplay | 异步 | 记日志，不改显示文本 |
| PreToolUse | 同步 | 记日志；命中规则则 deny；可选等待 |
| PermissionRequest | 同步 | 记日志；等待模式打开时等回复 |
| PermissionDenied | 异步 | 记日志 |
| PostToolUse | 异步 | 记日志，含截断后的工具输出 |
| PostToolUseFailure | 异步 | 记日志；notifier 打开时进 outbox |
| PostToolBatch | 异步 | 记日志 |
| Notification | 异步 | 记日志；notifier 打开时进 outbox |
| SubagentStart / SubagentStop | 异步 | 记日志 |
| TaskCreated / TaskCompleted | 异步 | 记日志 |
| Stop | 同步 | 记日志；notifier 打开时进 outbox |
| StopFailure | 同步 | 记日志；notifier 打开时进 outbox |
| TeammateIdle | 异步 | 记日志 |
| ConfigChange | 异步 | 记日志 |
| CwdChanged | 异步 | 记日志 |
| DirectoryAdded | 异步 | 记日志 |
| FileChanged | 异步 | 记日志。没有预置文件名，不主动 watch；等别的 hook 给出 `watchPaths` 才会响 |
| WorktreeCreate | 同步 | 记日志，并自己执行 `git worktree add`（见限制） |
| WorktreeRemove | 同步 | 记日志，并 `git worktree remove --force`。只接受 `.claude/worktrees/` 下的路径 |
| PreCompact / PostCompact | 异步 | 记日志，不阻断 compact |
| PreModelSwitch / PostModelSwitch | 异步 | 记日志 |
| Elicitation / ElicitationResult | 异步 | 记日志 |
| SessionEnd | 同步 | 记日志。timeout 5 秒，避免挤在 SessionEnd 默认的 1.5 秒预算里写不完 |

## 本机不支持的 hook 事件

没有。2.1.306 二进制里的合法事件数组和官方文档相同，就是上面这 33 个。`StatusLine` 和 `FileSuggestion` 出现在另一组名字里，它们不是 hook 事件，所以没有注册。

`claude plugin validate` 没有报告 unknown hook event。自测加载这个插件时，debug 日志里也没有 unknown hook event。

## Cursor 插件

`.cursor-plugin/plugin.json` 和 `.claude-plugin/` 都在仓库根上。Cursor 侧只有一个 skill：`claude-code-bridge`。内容是怎么生成 `CC_TASK_ID`、怎么用 `claude -p --output-format stream-json --verbose` 启动、推荐的权限参数（不用 bypass）、事件文件在哪、怎么读、怎么写 pending 回复、怎么判断结束、出错怎么办。

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

排除 `.cursor/`，避免本地插件目录里再带一份项目级 skill。然后在 Cursor 里 Reload Window，到 Customize 里确认 skill。这份副本给 IDE 用。self-hosted worker 不依赖它。

个人 skill 再复制一份（同样不用 symlink）：

```bash
mkdir -p ~/.cursor/skills/claude-code-bridge
cp cursor/skills/claude-code-bridge/SKILL.md ~/.cursor/skills/claude-code-bridge/SKILL.md
```

## 第 2 部分：worker 上能不能读到 skill

文档说 `~/.cursor/skills/` 留在本机，不会同步到 Cloud Agent、远程 SSH 或 self-hosted worker；worker 上应使用仓库里的项目级 skill，或把 skill 烤进 worker 镜像。

这个 worker 跑在用户自己的 Mac 上，文件系统就是那台机器。本会话启动时，注入的 skill 里已经有个人 skill `~/.cursor/skills/android-cli`，也有 `~/.cursor/skills-cursor/` 的内置 skill、`~/.claude/skills/`、`~/.codex/skills/`、`~/.agents/skills/`，以及 `~/.cursor/plugins/cache/` 里的插件 skill。当时还没有 `claude-code-bridge`，所以这次会话看不到它。新会话才会重新发现 skill。

因此三份副本的预期要分开验证，不能靠这次会话下结论：

| 副本 | 路径 | 按文档的预期 | 这次会话看到的 |
| --- | --- | --- | --- |
| 个人 skill | `~/.cursor/skills/claude-code-bridge/` | worker 不同步 | 同目录的 `android-cli` 在这次会话里是可见的。新 skill 要新会话才知道 |
| 项目 skill | 仓库 `.cursor/skills/claude-code-bridge/` | worker 可用 | 会话开始时文件还不存在 |
| IDE 本地插件 | `~/.cursor/plugins/local/cc-cursor-bridge/` | Reload 之后 IDE 的 Customize 能看到 | worker 会不会加载 local plugin，这次不能验证 |

工作区如果是 `/Users/timon/Sync/Test` 而不是仓库根，仓库里这份嵌套的 `.cursor/skills/` 只在处理 `cc-cursor-bridge/` 下面的文件时出现。要把 skill 装到工作区根上，用：

```bash
scripts/install_cursor_skill.sh /path/to/workspace
scripts/install_cursor_skill.sh --uninstall /path/to/workspace
```

脚本把 `cursor/skills/claude-code-bridge/` 整份（含以后加的 `scripts/`、`references/`）复制到 `<workspace>/.cursor/skills/claude-code-bridge/`，并且只替换这一个目录。工作区是 `/`、`$HOME`，或不存在时会拒绝。`--uninstall` 只删那个目录。本机 worker 的工作区根是 `/Users/timon/Sync/Test`，已用这个脚本装过；该目录不在本仓库的 git 里。

### 怎么验证

另起一个 agent，工作区用这个仓库，或在父目录里明确去读 `cc-cursor-bridge` 下的文件。请它回答：

1. 当前 skill 列表里有没有 `claude-code-bridge`。
2. 它看到的文件路径是 `.cursor/skills/...`、`~/.cursor/skills/...`，还是 `~/.cursor/plugins/local/cc-cursor-bridge/cursor/skills/...`。
3. 用 `/claude-code-bridge` 能否把 skill 正文引进来，并引用其中「不要用 bypass」那一段。

三份都出现时正文应当相同。项目级那份是 worker 的正式来源；个人 skill 和 IDE 插件副本是对照。

## 自测

单元测试不联网：

```bash
python3 scripts/test_bridge.py
/usr/bin/python3 scripts/test_bridge.py
```

两条都应通过。覆盖 `rm -rf`、`git push --force`、`.env` / `.env.example`、`~/.ssh`、日志脱敏、等待超时、notifier 只写 outbox、worktree 往返。

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
- 等待模式默认关。打开后，PreToolUse 的 allow 会跳过该次权限提示。
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
