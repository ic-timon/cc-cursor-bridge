---
name: claude-code-bridge
description: >-
  在本机启动 Claude Code（claude -p），用 cc-cursor-bridge 插件按 CC_TASK_ID
  读取 hook 事件日志，并在开启等待模式时通过 replies 文件批准或拒绝。
  当用户要求驱动本机 claude、查看 CC 事件、拦截危险命令，或回复 pending 审批时使用。
---

# Claude Code bridge

用本机的 `claude -p` 做实现，由仓库里的 Claude Code plugin 记事件、拦截危险操作。你负责生成任务号、启动进程、读事件、在需要时写回复。不要改用户的模型代理（本机是 cc-switch，`127.0.0.1:15721`），也不要改 `~/.claude/settings.json`。

## 生成 CC_TASK_ID

每次任务用一个新 id，整次 `claude` 进程都用它。只允许字母、数字、`.`、`_`、`-`。

```bash
export CC_TASK_ID="$(python3 -c 'import uuid; print(uuid.uuid4().hex)')"
export CC_BRIDGE_DIR="${CC_BRIDGE_DIR:-$HOME/.cc-bridge}"
```

事件文件在启动前就能确定：

`$CC_BRIDGE_DIR/tasks/$CC_TASK_ID/events.jsonl`

没设 `CC_TASK_ID` 时，插件改用 Claude 的 `session_id` 当目录名。那要等第一行日志出现才知道路径，所以始终自己设 `CC_TASK_ID`。

## 启动

`--plugin-dir` 指到含 `.claude-plugin/plugin.json` 和 `claude/hooks/hooks.json` 的检出。这台 self-hosted worker 上的 cloud agent 不会从仓库或工作区的 `.cursor/skills/`、`~/.cursor/plugins/local`、`~/.agents/skills/` 加载这份 skill。它只在 Cursor 里做过 Customize → Skills 的 “Sync Skills for Cloud Agents” 之后才能看到。同步会把 `~/.cursor/skills/claude-code-bridge/` 移进 AgentStores；之后改了 skill 要再同步一次。

```bash
claude -p \
  --plugin-dir /path/to/cc-cursor-bridge \
  --output-format stream-json \
  --verbose \
  --include-hook-events \
  --permission-mode acceptEdits \
  --permission-prompts none \
  --allowedTools "Write,Edit,Read,Glob,Grep" \
  "任务说明"
```

权限：

- `--permission-mode acceptEdits` 让文件编辑不用逐次确认。
- Bash 不要整类放行。只把这次任务真正要用的命令写进 `--allowedTools`，例如 `Bash(python3 *)`、`Bash(git status *)`、`Bash(git diff *)`。
- 不要用 `--dangerously-skip-permissions`、`--allow-dangerously-skip-permissions`，也不要 `--permission-mode bypassPermissions`。
- `--permission-prompts none` 让没人能回答的权限提示直接被 Claude Code 拒绝，避免 `-p` 卡住。插件的 PreToolUse 仍会先跑。
- 在没有审批界面的 `-p` 会话里，`acceptEdits` 也不一定会放行 Write，或放行会改文件的 Bash。只读命令（例如 `echo hi`）在 `--allowedTools Bash` 下可以执行。普通写入被 Claude Code 自己拒绝时，事件日志里的 PreToolUse 没有 `decision: deny`。这和插件拦截不是一回事。

把 `CC_TASK_ID` 和 `CC_BRIDGE_DIR` 放在同一条命令的环境里。需要边跑边读日志时，把 `claude` 放到后台或另开管道，不要等进程退出才去看事件文件。

## 事件文件怎么读

每一行是一个 JSON 对象，字段：

- `seq`：这个文件里的自增序号（加锁追加的顺序，异步 hook 完成得晚，序号就晚）
- `ts`：本地时间
- `event`：hook 名
- `session_id`
- `summary`：工具名、命令、路径、Edit 的 old/new、Bash 输出，长内容已截断
- `decision` / `rule_id` / `decision_reason`：这次有没有被规则或回复决定
- `pending_id`：若正在或已经走过等待通道
- `notify`：`disabled` 表示 notifier 没推；`queued_local` 表示只写了本地 outbox，没有 HTTP

增量读：记住上次看到的 `seq`，只处理更大的行。

```bash
tail -n +1 -f "$CC_BRIDGE_DIR/tasks/$CC_TASK_ID/events.jsonl"
```

`stream-json` 是模型输出。事件以 jsonl 为准。`--include-hook-events` 只是辅助。

## 危险操作

默认规则（`config/rules.json`，可关）会在 PreToolUse 直接 deny，不写 pending，也不等回复。`wait_all` 打开时也一样：命中规则就立刻拒绝，agent 的 reply 不能改写这次拒绝。

- `rm -rf` / `rm -fr` / `rm -r -f`，以及 PowerShell `Remove-Item -Recurse -Force`
- `git push --force`、`-f`、`--force-with-lease`
- 读写 `.env` 以及 `.env.*`，但放行 `.env.example`、`.env.sample`、`.env.template`
- 访问 `~/.ssh`（含展开后的绝对路径）

被拦之后换一条不触犯规则的路。不要试图绕过 hook，也不要改规则文件来放行，除非用户明确要求。

`rm -r`（没有 `-f`）和 `git push origin +branch` 这种 refspec 强制推送当前不拦。

## 回复 pending

33 个 hook 都已注册成同步命令，这样打开等待时脚本能拦住 Claude Code，直到你写回复。Claude Code 的命令 hook 默认超时上限：一般事件可以设到 600 秒；UserPromptSubmit、PreModelSwitch、PostModelSwitch 在不写 timeout 时默认被降到 30 秒，MessageDisplay 降到 10 秒；SessionEnd 共享预算最高 60 秒。本插件显式写了 timeout：普通事件 180 秒，SessionEnd 55 秒。等待超时要小于这些值。

全事件等待默认关。要让你来批每一次事件，启动前设：

```bash
export CC_BRIDGE_WAIT_ALL=1
export CC_BRIDGE_WAIT_ALL_TIMEOUT=90
```

对应配置是 `config/bridge.json` 的 `wait_all.enabled`。打开之后每个事件写：

`$CC_BRIDGE_DIR/tasks/$CC_TASK_ID/pending/<seq>-<事件名>.json`

并阻塞轮询：

`$CC_BRIDGE_DIR/tasks/$CC_TASK_ID/replies/<seq>.json`

`claude -p` 必须放后台，否则第一个 SessionStart 就会把你自己卡住。然后循环：

```bash
python3 /path/to/cc-cursor-bridge/scripts/bridge.py pending "$CC_TASK_ID"
python3 /path/to/cc-cursor-bridge/scripts/bridge.py reply "$CC_TASK_ID" <seq> '<json>'
```

回复是一个 JSON 对象：

- PreToolUse：`{"decision":"allow|deny|ask|defer","reason":"...","updatedInput":{...}}`
- PermissionRequest：`{"decision":"allow|deny","message":"...","updatedInput":{...}}`
- Stop、SubagentStop、PreCompact、TeammateIdle、TaskCreated、TaskCompleted：`{"decision":"block","reason":"..."}` 或 `{"ack":true}`
- UserPromptSubmit、UserPromptExpansion、PostToolUse：`{"decision":"block","reason":"..."}` 和/或 `{"additionalContext":"..."}`
- Elicitation、ElicitationResult：`{"decision":"accept|decline"}`
- WorktreeCreate：`{"worktreePath":"/absolute/path"}`，或 `{"ack":true}` 沿用 hook 已经建好的 git worktree
- 没有决定权的事件：`{"ack":true}`

超时按事件分：PreToolUse 和 PermissionRequest 默认 deny；Stop、SubagentStop、PreCompact 这类可阻断事件，以及没有决定权的事件，超时后不改原行为。SessionEnd 的 hook timeout 只有 55 秒，不要把 `CC_BRIDGE_WAIT_ALL_TIMEOUT` 设得比对应事件的 timeout 更大。

命令和文件内容超过上限时（默认命令 4000 字、文件内容 2000 字），短 pending 里该字段带 `truncated: true`、`original_length` 和 `sha256`，完整且已脱敏的正文在同目录的 `pending/<seq>-<事件>.full.json`。命令被截断时，先读这份 `.full.json` 再决定 allow。

旧的窄等待仍然可用：`CC_BRIDGE_WAIT=1` 只在 PermissionRequest 上等，`CC_BRIDGE_WAIT_PRE_TOOL=1` 才加上 PreToolUse。文件名是 `pending/<id>.json`，不是 seq。

等待模式默认关。关着的时候 PermissionRequest 只记日志，决定权在 Claude Code 自己的权限模式；PreToolUse 只在命中规则时 deny。

要让你来批，启动前打开等待（二选一）：

- 改插件目录里的 `config/bridge.json`：`wait.enabled` 设为 `true`。`on_permission_request` 默认会跟着生效。`on_pre_tool_use` 默认 false，设为 true 才对每次 PreToolUse 都等。
- 或者只对这一次进程设环境变量：`CC_BRIDGE_WAIT=1`。还要对 PreToolUse 等待时再加 `CC_BRIDGE_WAIT_PRE_TOOL=1`。超时秒数可用 `CC_BRIDGE_WAIT_TIMEOUT`。

打开之后，插件把请求写到：

`$CC_BRIDGE_DIR/tasks/$CC_TASK_ID/pending/<id>.json`

你在超时（默认 120 秒）之前写：

`$CC_BRIDGE_DIR/tasks/$CC_TASK_ID/replies/<id>.json`

```json
{"decision": "deny", "reason": "不要删除这个目录"}
```

`decision` 只能是小写 `allow` 或 `deny`。超时、文件不是合法 JSON、或 decision 不是这两个值，按配置默认 deny。

PreToolUse 上回 `allow` 会让这次工具调用跳过权限提示。只在你确认命令和路径安全时才 allow。

notifier 默认关，而且当前版本即使打开也只把打算发给 Cursor Cloud Agents API 的 followup 写到 `outbox/`，不发 HTTP。API key 只从环境变量 `CC_BRIDGE_CURSOR_API_KEY` 读取，禁止写进仓库、命令行回显或事件日志。

## 怎么判断结束

同时看这三处，以进程退出为准：

1. `claude` 进程退出码。
2. `stream-json` 里 `"type":"result"` 的那一行（成功时 `subtype` 一般为 `success`，失败时 `is_error` 为 true）。
3. 事件日志里的 `Stop`，随后应有同步写入的 `SessionEnd`。

全部 hook 都是同步的，日志会在 hook 返回前写完。`wait_all` 打开时，没写回复之前进程会停在对应事件上。

## 出错怎么办

- 任务目录或 `events.jsonl` 一直没有：`--plugin-dir` 没指到插件根，或 hook 找不到 `python3`。用 `--debug-file <日志> --debug=hooks` 重跑一次，看 hook 的 stderr。
- 流里出现 hook error：分发脚本把非 JSON 打到了 stdout，或脚本崩了。事件日志的 `summary.config_note` 会写配置回退。
- `StopFailure`：这一轮是 API 错误。看 `summary.error`，不要改用户的代理配置。
- pending 超时：插件已经按默认 deny。要重试就再跑一轮，或在超时前写入 reply。
- 工作区不是 git 仓库时，`WorktreeCreate` 会失败。这个 hook 替换了 Claude Code 自带的 worktree 行为，脚本自己执行 `git worktree add`，分支名是 `cc-bridge/<slug>`。
