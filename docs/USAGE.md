# 使用说明

给要在这台 Mac 上驱动本机 Claude Code 的人和 agent。命令按 Claude Code 文档核对过（[Install plugins](https://code.claude.com/docs/en/plugins/install)、[Create a marketplace](https://code.claude.com/docs/en/plugins/create-marketplace)）。

## 这是什么

这是一个 Claude Code 插件加一份 Cursor skill。插件把 Claude Code 的 hook 记下来，让 Cursor agent 能看到正在发生的事。默认有 4 个事件会停下来等你回复：`PreToolUse`（放行或拒绝工具）、`PermissionRequest`（权限请求）、`Elicitation`（接受或拒绝 MCP 提问）、`Stop`（验收，决定结束还是继续）。`config/rules.json` 命中的危险操作（`rm -rf`、强制 push、`.env`、`~/.ssh`）会立刻拒绝，不写 pending，回复也不能改写。

## 在 Mac 上安装

### Claude Code

一次任务用 `--plugin-dir`，不改全局配置：

```bash
claude --plugin-dir /path/to/cc-cursor-bridge -p "任务说明"
```

要装进 Claude Code，用仓库里的 `.claude-plugin/marketplace.json`。marketplace 的名字是文件里的 `"name"`，也就是 `cc-cursor-bridge`。这条会把 marketplace 写进 Claude Code 的用户设置；不想改设置就只用上面的 `--plugin-dir`。

本地目录（绝对路径，或在会话里用 `./` 开头的相对路径）：

```bash
claude plugin marketplace add /path/to/cc-cursor-bridge
claude plugin install cc-cursor-bridge@cc-cursor-bridge
```

已经打开 Claude Code 时：

```text
/plugin marketplace add /path/to/cc-cursor-bridge
/plugin install cc-cursor-bridge@cc-cursor-bridge
```

从 GitHub 一次装上（私有仓库要有读权限）。`--marketplace` 后面是仓库来源，插件名不加 `@`：

```text
/plugin install cc-cursor-bridge --marketplace ic-timon/cc-cursor-bridge
```

等价的两步：

```bash
claude plugin marketplace add ic-timon/cc-cursor-bridge
claude plugin install cc-cursor-bridge@cc-cursor-bridge
```

### Cursor

Skill 只有一份：`cursor/skills/claude-code-bridge/SKILL.md`。

```bash
scripts/install_cursor_skill.sh
```

然后在 Cursor 里打开 Customize → Skills → Sync Skills for Cloud Agents。同步会把 `~/.cursor/skills/claude-code-bridge/` 移进 AgentStores。以后每改一次 `SKILL.md`，都要再跑安装脚本，再同步一次。

这台 self-hosted worker 上已经核对过：cloud agent 不加载项目或工作区的 `.cursor/skills`、`~/.agents/skills`、`~/.cursor/plugins/local`。`--workspace` 只给本机 IDE，cloud agent 读不到。

## 让 cloud agent 来用

把下面这段贴给这台 worker 上的 Cursor cloud agent，换成你的目录和任务：

```text
用 claude-code-bridge 让本机 Claude Code 在 <dir> 里实现 <任务>，按默认策略审批。危险命令直接拒绝。PreToolUse 和 PermissionRequest 看完再 allow。仓库路径里的 Write 如果被 safetyCheck 挡住，用 PermissionRequest allow。Stop 时验收：测试或要求没满足就 block，并写明缺什么。不要用 bypass 权限，不要改 cc-switch。
```

Agent 会做这些事：用 `--plugin-dir` 在后台启动 `claude -p`，循环 `pending` 然后 `reply`，用 `push` 批量读通知，在 Stop 上做验收。

## 自己用命令行

### 环境变量

```bash
export CC_TASK_ID="$(python3 -c 'import uuid; print(uuid.uuid4().hex)')"
export CC_BRIDGE_DIR="${CC_BRIDGE_DIR:-$HOME/.cc-bridge}"
```

| 变量 | 作用 |
| --- | --- |
| `CC_TASK_ID` | 任务目录名。只允许字母、数字、`.`、`_`、`-`。不设就用 Claude 的 `session_id` |
| `CC_BRIDGE_DIR` | 日志根，默认 `~/.cc-bridge` |
| `CC_BRIDGE_WAIT_ALL` | `1` 时 33 个事件都写 pending 并等回复，用来调试。异步 hook 本身不挡 Claude Code |
| `CC_BRIDGE_WAIT_ALL_TIMEOUT` | 等待秒数，默认 120。要小于对应 hook 的 timeout |
| `CC_BRIDGE_EVENT_MODE_<事件名>` | 覆盖该事件：`wait`、`push`、`log`、`off`。例：`CC_BRIDGE_EVENT_MODE_Stop=log` |
| `CC_BRIDGE_ASSISTANT_TEXT` | `1` 时把 `MessageDisplay` 从 `log` 改成 `push`，实时带出助手文本。默认关；也受 `assistant_text.enabled` 配置控制 |
| `CC_BRIDGE_WATCH` | 用系统路径分隔符列出要监视的文件。SessionStart 会注入 `watchPaths`，并且不等回复 |

日志在 `$CC_BRIDGE_DIR/tasks/$CC_TASK_ID/events.jsonl`。

### pending、reply、push、watch

`claude -p` 放后台。先处理会卡住的 pending，再批量读 push。把下面的路径换成仓库检出：

```bash
python3 /path/to/cc-cursor-bridge/scripts/bridge.py pending "$CC_TASK_ID"
python3 /path/to/cc-cursor-bridge/scripts/bridge.py reply "$CC_TASK_ID" 4 '{"decision":"allow","reason":"只读，允许"}'
python3 /path/to/cc-cursor-bridge/scripts/bridge.py push "$CC_TASK_ID"
python3 /path/to/cc-cursor-bridge/scripts/bridge.py push "$CC_TASK_ID" --since 0
python3 /path/to/cc-cursor-bridge/scripts/bridge.py watch "$CC_TASK_ID"
```

`pending` 只列出还没有回复的等待项。`push` 列出还没消费的通知并前进游标；`--since <seq>` 从该序号之后再列一次。`pending` 和 `push` 每行第一列就是完整路径，没有前缀；有摘要时在同一行路径后接一个 TAB 和 `# ...`，没有摘要时与以前逐字节一致。`watch` 每出现一条就打一行：`pending <路径>` 或 `push <路径>`，摘要同样用 TAB 接在路径后。摘要格式：Stop/SubagentStop `# reply: <回答第一行>`，PostToolUse `# response: <输出第一行>`，PostToolUseFailure `# error: <错误第一行>`，助手文本打开时 `# assistant: <文本第一行>`。加 `--once` 只打当前这一批。

命令、文件内容、Stop/SubagentStop 的回答、PostToolUse 的 `tool_response` 或 PostToolUseFailure 的 `error` 超过上限时，短文件里该字段是一个带 `text`、`truncated: true`、`original_length` 和 `sha256` 的对象。完整且已脱敏的正文在同目录的 `<seq>-<事件>.full.json`。先读这份完整文件，再决定 allow 或验收。主通知里的 `summary` 仍按 `log.max_field_chars`（默认 240）保持简短。

### 四个 WAIT 事件的回复

```bash
# PreToolUse
python3 /path/to/cc-cursor-bridge/scripts/bridge.py reply "$CC_TASK_ID" <seq> '{"decision":"allow","reason":"看过完整命令"}'
python3 /path/to/cc-cursor-bridge/scripts/bridge.py reply "$CC_TASK_ID" <seq> '{"decision":"deny","reason":"不要跑这条"}'

# PermissionRequest。仓库内 Write 被 safetyCheck 挡住时，用 allow
python3 /path/to/cc-cursor-bridge/scripts/bridge.py reply "$CC_TASK_ID" <seq> '{"decision":"allow","message":"允许写入这个文件"}'

# Elicitation
python3 /path/to/cc-cursor-bridge/scripts/bridge.py reply "$CC_TASK_ID" <seq> '{"decision":"accept"}'

# Stop。没满足就 block，满足了 ack
python3 /path/to/cc-cursor-bridge/scripts/bridge.py reply "$CC_TASK_ID" <seq> '{"decision":"block","reason":"补一个 n=30 的测试再跑"}'
python3 /path/to/cc-cursor-bridge/scripts/bridge.py reply "$CC_TASK_ID" <seq> '{"ack":true}'
```

`PreToolUse` 的 `decision` 还可以是 `ask` 或 `defer`。超时：PreToolUse 和 PermissionRequest 拒绝，Elicitation 拒绝提问，Stop 放行结束。

### 回答和工具返回

Stop 和 SubagentStop 的 pending/push 通知顶层会多这些字段：

- `last_assistant_message`：CC 最后一条回答。超过 `pending.content_chars`（默认 2000）时是截断对象，完整内容在 `.full.json`
- `transcript_path`：会话 transcript 路径
- SubagentStop 还会带 `agent_transcript_path`、`agent_type`、`agent_id`（事件里存在时）

PostToolUse 的通知带 `tool_response`，PostToolUseFailure 带 `error` 和 `is_interrupt`。它们在主通知里按 `log.max_field_chars`（默认 240）截断，完整内容同样进 `.full.json`。这些字段和日志里的其他文本一样会脱敏。

`bridge.py pending/push` 的每一行第一列是路径，后面用 TAB 追加第一行预览；`watch` 在路径前加 `pending`/`push`：

```text
~/.cc-bridge/tasks/<task>/push/12-Stop.json	# reply: 已完成 fizzbuzz，测试通过
~/.cc-bridge/tasks/<task>/push/13-PostToolUse.json	# response: {"stdout": "hi\n", "stderr": ""}
```

### 助手文本（默认关）

`config/bridge.json` 的 `assistant_text.enabled` 默认 `false`。设成 `true`，或者单次用 `CC_BRIDGE_ASSISTANT_TEXT=1`，`MessageDisplay` 会从 `log` 改成 `push`，通知里带：

- `assistant_text`：这一段的助手文本（事件的 `delta`）
- `message_id`、`turn_id`、`index`、`final`：把片段按消息归组；`final: true` 表示这条消息结束
- `transcript_path`

为什么用 MessageDisplay 而不是读 transcript：Claude Code 2.1.306 的 MessageDisplay 输入里已经有 `delta`、`message_id`、`final`，直接把它转成 push 就能实时看到，不用按任务维护 transcript 偏移量，也避开了文件增长、半行 JSON 和多个异步 hook 并发读取的竞争。`delta` 是片段，监督者按 `message_id` 聚合即可；某段超过 `log.max_field_chars` 时主通知截断，完整文本在 `.full.json`。

开关关闭时 MessageDisplay 仍是 `log`，行为与之前一致。`CC_BRIDGE_EVENT_MODE_MessageDisplay=off/push/wait` 这类显式覆盖仍然生效：开关只把解析结果是 `log` 的 MessageDisplay 改成 `push`。`CC_BRIDGE_WAIT_ALL=1` 时每个事件都走 pending，MessageDisplay 也一样，不再被开关改写。

## 边跑边看

一个终端盯文件通道：

```bash
python3 /path/to/cc-cursor-bridge/scripts/bridge.py watch "$CC_TASK_ID"
```

另一个终端看 Claude 自己的流。`--include-hook-events` 只是辅助，以 jsonl 为准：

```bash
claude -p \
  --plugin-dir /path/to/cc-cursor-bridge \
  --output-format stream-json \
  --verbose \
  --include-hook-events \
  --permission-mode acceptEdits \
  "任务说明"
```

结束看进程退出码，以及流里 `"type":"result"` 那一行。Stop 被 ack 之后进程才会真正停。

## 配置

### 事件模式和超时

`config/bridge.json` 的 `events` 给每个事件一个模式：`wait`、`push`、`log`、`off`。默认 wait 的是上面那 4 个。push 写 `push/<seq>-<事件>.json` 和 `push/stream.jsonl`，不阻塞。log 只追加 `events.jsonl`。

`assistant_text.enabled` 默认 `false`；打开后 MessageDisplay 从 log 变成 push，详见上面的「助手文本」。

WAIT 的 hook timeout 是 180 秒。等待本身默认 120 秒（`wait_all.timeout_seconds`，或 `CC_BRIDGE_WAIT_ALL_TIMEOUT`）。SessionEnd 的 timeout 是 55 秒。`stop.max_blocks` 默认 3。

失败时：PreToolUse、PermissionRequest 拒绝；Elicitation 以 decline 回答；Stop 放行结束。`stop_hook_active` 为 true 且回复里没有明确的 `decision: block` 时，也放行结束。

### 加一条规则

编辑 `config/rules.json` 的 `rules` 数组，放在最前面的先生效。`enabled: false` 可以关掉一条。`command_regex` 和 `path_regex` 是通用入口：

```json
{
  "id": "bash-sudo",
  "enabled": true,
  "tools": ["Bash"],
  "reason": "不要用 sudo",
  "command_regex": "(^|[;&|\\n])\\s*sudo\\b"
}
```

命中后立刻 deny，不写 pending。换一份规则文件用 `CC_BRIDGE_RULES=/path/to/rules.json`。

## 容易踩的坑

- 仓库路径里的 Write 会被 Claude Code 的 safetyCheck 当成敏感文件。PreToolUse allow 挡不住，要在 PermissionRequest 上 allow。仓库外的同一条 Write 通常不用这一步。
- 这 5 个事件在 `-p` 里没有触发出来：PermissionDenied、StopFailure、TeammateIdle、CwdChanged、DirectoryAdded。auto 模式没有稳定地拒绝命令；把 API 指到没人听的端口仍被本机代理接走；没有可用的 agent teams 开关；Bash `cd` 不改变会话目录；`/add-dir` 在这个环境里不可用。
- Stop 最多 block 3 次。第 4 次即使再 block 也会结束。
- 助手文本按 MessageDisplay 的 `delta` 推送，是片段而不是完整消息；要完整句子就按 `message_id` 聚合，或等 `final: true`。
- 一次 fizzbuzz（含一次 Stop block，补了 n=30 的测试）大约 $0.04，7 个 pending，7 轮，大约 47 秒。

## 安全原则

- 不要用 `--dangerously-skip-permissions`、`--allow-dangerously-skip-permissions` 或 `--permission-mode bypassPermissions`。
- 不监听端口，不发 HTTP。notifier 即使打开也只写本地 `outbox/`。
- 跑任务时不改 `~/.claude/settings.json`，不改 cc-switch。模型密钥留在 cc-switch（本机是 `127.0.0.1:15721`）。
- 不把 API key 写进仓库、命令行回显或事件日志。
