# 使用说明

给要在这台 Mac 上驱动本机 Claude Code 的人和 agent。命令按 Claude Code 文档核对过（[Install plugins](https://code.claude.com/docs/en/plugins/install)、[Create a marketplace](https://code.claude.com/docs/en/plugins/create-marketplace)）。

## 这是什么

这是一个 Claude Code 插件加一份 Cursor skill。插件把 Claude Code 的 hook 记下来，让 Cursor agent 能看到正在发生的事。默认有 4 个事件会停下来等你回复：`PreToolUse`（放行或拒绝工具）、`PermissionRequest`（权限请求）、`Elicitation`（接受或拒绝 MCP 提问）、`Stop`（验收，决定结束还是继续）。`config/rules.json` 命中的危险操作（`rm -rf`、强制 push、`.env`、`~/.ssh`、`.claude-flow/`、`.swarm/`）会立刻拒绝，不写 pending，回复也不能改写。

## 在 Mac 上安装

### Claude Code

一次任务用 `--plugin-dir`，不改全局配置。本机 live 检出是 `~/Sync/Test/cc-cursor-bridge` 的 **`main`**（不要写死旧功能分支名，例如曾经的 `feat/plugin-skeleton`）。带 `-p` 时 prompt 走 stdin，不要写在参数末尾。完整启动见下面的「启动（prompt 走 stdin）」。

```bash
export CC_TASK_ID="$(python3 -c 'import uuid; print(uuid.uuid4().hex)')"
~/Sync/Test/cc-cursor-bridge/scripts/run_claude_bridged.sh \
  --prompt-file prompt.txt \
  --allowed-tools 'Write,Edit,Read,Glob,Grep'
```

要装进 Claude Code，用仓库里的 `.claude-plugin/marketplace.json`。marketplace 的名字是文件里的 `"name"`，也就是 `cc-cursor-bridge`。这条会把 marketplace 写进 Claude Code 的用户设置；不想改设置就只用上面的 `--plugin-dir`。

本地目录（绝对路径，或在会话里用 `./` 开头的相对路径）：

```bash
claude plugin marketplace add ~/Sync/Test/cc-cursor-bridge
claude plugin install cc-cursor-bridge@cc-cursor-bridge
```

已经打开 Claude Code 时：

```text
/plugin marketplace add ~/Sync/Test/cc-cursor-bridge
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
用 claude-code-bridge 让本机 Claude Code 在 <dir> 里实现 <任务>，按默认策略审批。插件用 --plugin-dir 指到 ~/Sync/Test/cc-cursor-bridge（main，不要写死 feat/plugin-skeleton 等旧分支）。用 scripts/run_claude_bridged.sh 启动，prompt 放文件从 stdin 送入；不要把任务说明写在 --allowedTools 后面。危险命令直接拒绝。PreToolUse 和 PermissionRequest 看完再 allow。仓库路径里的 Write 如果被 safetyCheck 挡住，用 PermissionRequest allow。Stop 时验收：测试或要求没满足就 block，并写明缺什么；若缺的是这次会话解不了的外部依赖（例如 CI runner 离线、公平队列被压测占满），不要反复用同一理由 block Stop，也不要抬高 CC_BRIDGE_STOP_MAX_BLOCKS 空转——ack 结束，把缺口写进 PR/报告。不要用 bypass 权限，不要改 cc-switch。提交前不要 git add -A（.claude-flow/、.swarm/ 等产物目录会被默认规则拒绝，也不该进 PR）。
```

Agent 会做这些事：用 `scripts/run_claude_bridged.sh`（`--plugin-dir` 指向 `~/Sync/Test/cc-cursor-bridge`）在后台启动 `claude -p`，prompt 走 stdin，循环 `pending` 然后 `reply`，用 `push` 批量读通知，在 Stop 上做验收。

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
| `CC_BRIDGE_AHP_DIR` | AHP v1 目录根，默认 `$CC_BRIDGE_DIR/ahp` |
| `CC_BRIDGE_AHP` | `0` 时不写 AHP 目录（只写旧格式）。默认 `1` |
| `CC_BRIDGE_LEGACY` | `0` 时不写旧格式目录（只写 AHP）；`pending`/`push`/`watch`/`reply` 也随之走 AHP 目录。默认 `1` |

日志在 `$CC_BRIDGE_DIR/tasks/$CC_TASK_ID/events.jsonl`。AHP v1 的同一批事件在 `$CC_BRIDGE_DIR/ahp/tasks/$CC_TASK_ID/`，见下面的「迁移到 AHP v1（0.2.0）」。

### 启动（prompt 走 stdin）

`claude -p` 的 prompt 用文件重定向，或用 `scripts/run_claude_bridged.sh`。脚本会设好 `CC_TASK_ID`（未设就生成），`--plugin-dir` 指向本仓库，把 Claude 的标准输出写到 `~/cc-$CC_TASK_ID.jsonl`，标准错误写到 `~/cc-$CC_TASK_ID.err`。prompt 只从 `--prompt-file` 或 stdin 读，不会放进 argv。

```bash
export CC_TASK_ID="$(python3 -c 'import uuid; print(uuid.uuid4().hex)')"
export CC_BRIDGE_DIR="${CC_BRIDGE_DIR:-$HOME/.cc-bridge}"
~/Sync/Test/cc-cursor-bridge/scripts/run_claude_bridged.sh \
  --cwd /path/to/repo \
  --prompt-file prompt.txt \
  --allowed-tools 'Write,Edit,Read,Glob,Grep'
```

不用脚本时，同样把 prompt 放在重定向里，consuming flag 放在命令上、不要放在 prompt 前面等着去吃它：

```bash
claude -p \
  --plugin-dir ~/Sync/Test/cc-cursor-bridge \
  --output-format stream-json \
  --verbose \
  --include-hook-events \
  --permission-mode acceptEdits \
  --permission-prompts none \
  --allowedTools 'Write,Edit,Read,Glob,Grep' \
  < prompt.txt \
  > ~/cc-$CC_TASK_ID.jsonl \
  2> ~/cc-$CC_TASK_ID.err
```

`--allowedTools`、`--disallowedTools` 以及其它会吃掉后续参数直到下一个选项的 flag，是 consuming flag。它们不能夹在 `-p` 和 prompt 之间，也不能排在末尾那个 prompt 参数前面。kio 的一次启动写成 `claude -p --plugin-dir <repo> --allowedTools … "<prompt>"`，prompt 被 `--allowedTools` 吃掉，Claude 的 stderr 是 `Input must be provided either through stdin or as a prompt argument`。恢复时把 prompt 放进文件从 stdin 送入，并换一个新的 `CC_TASK_ID`。

不要这样写：

```bash
claude -p --plugin-dir ~/Sync/Test/cc-cursor-bridge --allowedTools "Write,Edit" "任务说明"
```

只有在 prompt 是 `-p` 的紧邻下一个参数、且后面不再跟 consuming flag 时，才可以不用重定向：`claude -p "$(cat prompt.txt)" --plugin-dir …`。文档里的启动例子一律走 stdin。`run_claude_bridged.sh --dry-run` 只打印 argv，并注明 prompt 走 stdin，不需要本机有 `claude`。

### pending、reply、push、watch

`claude -p` 放后台。先处理会卡住的 pending，再批量读 push。把下面的路径换成仓库检出：

```bash
python3 ~/Sync/Test/cc-cursor-bridge/scripts/bridge.py pending "$CC_TASK_ID"
python3 ~/Sync/Test/cc-cursor-bridge/scripts/bridge.py reply "$CC_TASK_ID" 4 '{"decision":"allow","reason":"只读，允许"}'
python3 ~/Sync/Test/cc-cursor-bridge/scripts/bridge.py push "$CC_TASK_ID"
python3 ~/Sync/Test/cc-cursor-bridge/scripts/bridge.py push "$CC_TASK_ID" --since 0
python3 ~/Sync/Test/cc-cursor-bridge/scripts/bridge.py watch "$CC_TASK_ID"
```

`pending` 只列出还没有回复的等待项。回复已经生效，或者超时已经作出决定之后，对应的 `pending/<id>.json`（以及截断时的 `.full.json`）会删掉；文件本来就不在就忽略。`replies/` 和 `events.jsonl` 留下来。同一次决定再追加一行到任务目录的 `approvals.jsonl`（旧目录和 AHP 目录各一份）：`ts`、`seq` 或 `pending_id`、`event`、`tool`（有工具名时）、`decision`、`via`（`rule`、`reply`、`timeout`，以及 `invalid_reply` / `error` / `cwd_read`）、`reason`。`via` 是 `rule` 时还有 `rule_id`。`reason` 和 `tool` 的脱敏方式与 `events.jsonl` 相同。规则直接拒绝、人的回复、超时默认都会写一行。`push` 列出还没消费的通知并前进游标；`--since <seq>` 从该序号之后再列一次。`pending` 和 `push` 每行第一列就是完整路径，没有前缀；有摘要时在同一行路径后接一个 TAB 和 `# ...`，没有摘要时与以前逐字节一致。`watch` 每出现一条就打一行：`pending <路径>` 或 `push <路径>`，摘要同样用 TAB 接在路径后。摘要格式：Stop/SubagentStop `# reply: <回答第一行>`，PostToolUse `# response: <输出第一行>`，PostToolUseFailure `# error: <错误第一行>`，助手文本打开时 `# assistant: <文本第一行>`。加 `--once` 只打当前这一批。

命令、文件内容、Stop/SubagentStop 的回答、PostToolUse 的 `tool_response` 或 PostToolUseFailure 的 `error` 超过上限时，短文件里该字段是一个带 `text`、`truncated: true`、`original_length` 和 `sha256` 的对象。完整且已脱敏的正文在同目录的 `<seq>-<事件>.full.json`。先读这份完整文件，再决定 allow 或验收。主通知里的 `summary` 仍按 `log.max_field_chars`（默认 240）保持简短。

### 四个 WAIT 事件的回复

```bash
# PreToolUse
python3 ~/Sync/Test/cc-cursor-bridge/scripts/bridge.py reply "$CC_TASK_ID" <seq> '{"decision":"allow","reason":"看过完整命令"}'
python3 ~/Sync/Test/cc-cursor-bridge/scripts/bridge.py reply "$CC_TASK_ID" <seq> '{"decision":"deny","reason":"不要跑这条"}'

# PermissionRequest。仓库内 Write 被 safetyCheck 挡住时，用 allow
python3 ~/Sync/Test/cc-cursor-bridge/scripts/bridge.py reply "$CC_TASK_ID" <seq> '{"decision":"allow","message":"允许写入这个文件"}'

# Elicitation
python3 ~/Sync/Test/cc-cursor-bridge/scripts/bridge.py reply "$CC_TASK_ID" <seq> '{"decision":"accept"}'

# Stop。没满足就 block，满足了 ack
python3 ~/Sync/Test/cc-cursor-bridge/scripts/bridge.py reply "$CC_TASK_ID" <seq> '{"decision":"block","reason":"补一个 n=30 的测试再跑"}'
python3 ~/Sync/Test/cc-cursor-bridge/scripts/bridge.py reply "$CC_TASK_ID" <seq> '{"ack":true}'
# 外部依赖这次解不了（runner 离线等）：ack，缺口写进 PR；不要同一理由反复 block，也不要抬 CC_BRIDGE_STOP_MAX_BLOCKS
```

`PreToolUse` 的 `decision` 还可以是 `ask` 或 `defer`。超时：PreToolUse 和 PermissionRequest 拒绝，Elicitation 拒绝提问，Stop 放行结束。

从 0.2.0 起，回复不合法时和超时一样按默认决定处理，**不再交给 Claude Code 自己的权限流程**：`decision` 拼错或不在允许范围、有未知字段（例如 `updatedinput`）、坏 JSON、Stop 的 `block` 没写 `reason`、别名和正式字段冲突（`{"decision":"allow","behavior":"deny"}`）。`bridge.py reply` 会先按同样的规则校验，不合法就 exit 2、什么都不写；同一个 seq 已有不同的回复也 exit 2，重写一模一样的回复 exit 0。

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
python3 ~/Sync/Test/cc-cursor-bridge/scripts/bridge.py watch "$CC_TASK_ID"
```

另一个终端看 Claude 自己的流（`run_claude_bridged.sh` 写到 `~/cc-$CC_TASK_ID.jsonl`，标准错误在 `~/cc-$CC_TASK_ID.err`）。`--include-hook-events` 只是辅助，以 bridge 的 jsonl 为准：

```bash
tail -f ~/cc-$CC_TASK_ID.jsonl
```

结束看进程退出码，以及流里 `"type":"result"` 那一行。Stop 被 ack 之后进程才会真正停。

## 配置

### 事件模式和超时

`config/bridge.json` 的 `events` 给每个事件一个模式：`wait`、`push`、`log`、`off`。默认 wait 的是上面那 4 个。push 写 `push/<seq>-<事件>.json` 和 `push/stream.jsonl`，不阻塞。log 只追加 `events.jsonl`。

`assistant_text.enabled` 默认 `false`；打开后 MessageDisplay 从 log 变成 push，详见上面的「助手文本」。

`auto_allow.cwd_read` 默认 `false`（不写这份配置也一样）。设成 `true`，或单次 `CC_BRIDGE_AUTO_ALLOW_CWD_READ=1`，PreToolUse 的 `Read` 在目标路径解析之后仍落在 Claude 会话 cwd 里面、并且没有命中 `rules.json` 时，立刻 allow，不写 pending。cwd 外面的路径、通过符号链接跳到 cwd 外面的路径、以及 Read 以外的工具，仍要等待或走规则。`.env`、`~/.ssh`、`.claude-flow/`、`.swarm/` 先被规则拒绝，自动放行不会盖过它们。`CC_BRIDGE_AUTO_ALLOW_CWD_READ=0` 可以在配置打开时把这一次关掉。

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

命中后立刻 deny，不写 pending，回复也不能改写（和 `rm -rf` / `.env` / `~/.ssh` 一样）。默认规则里的 `agent-artifact-dirs` 会拦 `.claude-flow/`、`.swarm/`：PR 前不要 `git add -A`，以免把这些产物目录带进提交。换一份规则文件用 `CC_BRIDGE_RULES=/path/to/rules.json`。

## 迁移到 AHP v1（0.2.0）

0.2.0 起 bridge 是 AHP v1（`ahp/1`，见 `spec/ahp-v1.md`）的 producer。默认**双写**：

- 旧目录 `$CC_BRIDGE_DIR/tasks/<task>/` 不变：文件名 `<seq>-<Hook>.json`、`events.jsonl` 的字段、`pending`/`push`/`watch` 的输出（路径在第一列，TAB 后接 `# ...`）都和以前一样。现有 bot 不用改。
- 新目录 `$CC_BRIDGE_DIR/ahp/tasks/<task>/`（`CC_BRIDGE_AHP_DIR` 可改）：`capabilities.json`、`consumer.json`（consumer 写）、`pending/<seq>-<事件>.json`、`push/` 和 `push/stream.jsonl`、`replies/<seq>.json`、`acks/<seq>.json`、`events.jsonl`、`state/`。

两边共用一个 seq。只在 AHP 里有的事件（`policy.deny`、`producer.error`、`plan.update`）也占 seq，所以旧目录里的 seq 会有空号。

回复写哪边都行，hook 两边都看，先到先用。旧别名（`behavior`、`message`、`updatedInput`、`{"ack":true}`、Elicitation 的 `allow`/`deny`）两边都接受。新代码请写 AHP 字段：

```bash
python3 scripts/bridge.py pending "$CC_TASK_ID" --ahp
python3 scripts/bridge.py reply "$CC_TASK_ID" 4 '{"decision":"allow","reason":"只读"}' --ahp
python3 scripts/bridge.py push "$CC_TASK_ID" --ahp      # 游标在 consumer/push-cursor
python3 scripts/bridge.py watch "$CC_TASK_ID" --ahp
```

`--ahp` 下 `pending` 只列还没有回复、也还没有 ack 的项；`reply` 写规范化后的 AHP 字段（带 `protocol`、`seq`）。每个 gate 结束时 `acks/<seq>.json` 写明实际执行的决定：`status` 是 `consumed`/`expired`/`invalid`，`via` 是 `reply`/`timeout`/`rule`/`error`/`invalid_reply`；Stop 另有 `accepted`。

行为变化（0.2.0，都是 fail-closed 方向）：

| 以前 | 现在 |
| --- | --- |
| hook 脚本崩溃或 stdin 不是 JSON：exit 0、无输出，Claude Code 走自己的权限流程 | PreToolUse/PermissionRequest 输出 deny，Elicitation 输出 decline，Stop 以 exit 0 空输出结束本轮；写 ack（`via: error`）和 `producer.error`。hooks.json 把 hook 名作为参数传给 `dispatch.py`，所以 stdin 坏了也知道是哪个 hook；不知道时 exit 2 |
| 不合法的回复（拼错的 `decision`、PreToolUse 上的 `{"ack":true}`、未知字段）：空输出，等于没表态 | 按默认决定：deny / decline / Stop 结束；ack `status: invalid` |
| 规则里的正则写错：跳过这条规则 | 这条规则对它管的工具 deny，并发 `producer.error` |
| `updatedInput` 改写后直接执行 | 改写后的输入重跑规则，命中就 deny（ack `via: rule`，发 `policy.deny`） |
| `reply` 什么都写 | 先校验，不合法 exit 2 |
| 等待秒数不封顶 | 封顶 170 秒（同步 hook 的 180 秒减 10 秒余量） |
| 只脱敏 3 个环境变量的值 | 另外脱敏令牌格式：`sk-…`、`ghp_…`、`github_pat_…`、`xox?-…`、`AKIA…`、PEM 私钥 |
| 通知里的 `transcript_path` 也会被截断 | 路径不截断；UserPromptSubmit 通知带 `prompt`，Elicitation 通知带 `message`/`mcp_server_name`/`requested_schema` |
| 临时文件 `<name>.tmp` | `.<name>.<pid>.<随机>.tmp` |
| 旧窄等待（`CC_BRIDGE_WAIT`）的回复不合法：忽略，一直等到超时 | 立即按 `wait.timeout_decision`（缺省 deny）处理；AHP 目录里这是一个普通 gate（pending + ack），旧目录仍只有 `pending/<tool_use_id>.json`，两边的回复都认 |
| 旧目录通知里的截断对象只有 `text`/`truncated`/`original_length`/`sha256`，完整版要看顶层 `full_file` | 截断对象里也带 `full_file`（只多一个字段） |
| `additionalContext` 多长都照发（CC 超过 10,000 字符会静默换成文件路径加 2,000 字符预览） | 超过 10,000（按 UTF-16 码元计，bridge JSON `ahp.max_additional_context_chars` 只能调小）就是无效回复，按默认决定处理，不截断。只有等待项（gate）的回复会注入；对 push 通知写的回复不会注入，AHP 目录里会给它一个 `status: invalid` 的 ack。回复里带了该事件不接受的字段（例如 Stop 带 `additionalContext`）也算无效 |

第三方工具（例如 MCP 的 shell 工具）可以在 bridge JSON 里声明 `tool_kind`，规则的 `tool_kinds` 和 `tool.pre` 里的 `command` 就能覆盖它：

```json
{"ahp": {"tool_kinds": {"mcp__box__run": {"kind": "shell", "argv": "argv"}}}}
```

只想要一边：`CC_BRIDGE_AHP=0` 只写旧格式；`CC_BRIDGE_LEGACY=0` 只写 AHP（这时 CLI 默认就用 AHP 目录）。

Claude Code 的 hook 不给推理（thinking）内容，所以 capabilities 里 `features.reasoning_delta` 固定为 `false`；`consumer.json` 请求它时记为 `partially_applied` 并写明原因。

检查是否符合规范：`python3 -m conformance --producer cc-adapter`。

## 容易踩的坑

- `--allowedTools` 会吃掉它后面的参数，直到下一个选项。prompt 写在它后面时到不了 `-p`，stderr 是 `Input must be provided either through stdin or as a prompt argument`（kio 的 TLS agent 第一次启动）。启动一律用 stdin：`scripts/run_claude_bridged.sh --prompt-file …`，或 `claude -p … < prompt.txt`。详见「启动（prompt 走 stdin）」。
- `pending/` 里已经回复或已经超时的文件会被删掉，避免 `ls pending` 把旧请求当成还在等。审计看 `replies/`、`events.jsonl` 和 `approvals.jsonl`。
- 仓库路径里的 Write 会被 Claude Code 的 safetyCheck 当成敏感文件。PreToolUse allow 挡不住，要在 PermissionRequest 上 allow。仓库外的同一条 Write 通常不用这一步。
- 这 5 个事件在 `-p` 里没有触发出来：PermissionDenied、StopFailure、TeammateIdle、CwdChanged、DirectoryAdded。auto 模式没有稳定地拒绝命令；把 API 指到没人听的端口仍被本机代理接走；没有可用的 agent teams 开关；Bash `cd` 不改变会话目录；`/add-dir` 在这个环境里不可用。
- Stop 最多 block 3 次（`stop.max_blocks` / `CC_BRIDGE_STOP_MAX_BLOCKS`，默认 3）。第 4 次即使再 block 也会结束。缺的是这次会话修不了的外部依赖时，应 `ack` 并把缺口写进 PR/报告；不要靠抬高 `CC_BRIDGE_STOP_MAX_BLOCKS` 空转。
- 助手文本按 MessageDisplay 的 `delta` 推送，是片段而不是完整消息；要完整句子就按 `message_id` 聚合，或等 `final: true`。
- 一次 fizzbuzz（含一次 Stop block，补了 n=30 的测试）大约 $0.04，7 个 pending，7 轮，大约 47 秒。

## 安全原则

- 不要用 `--dangerously-skip-permissions`、`--allow-dangerously-skip-permissions` 或 `--permission-mode bypassPermissions`。
- 不监听端口，不发 HTTP。notifier 即使打开也只写本地 `outbox/`。
- 跑任务时不改 `~/.claude/settings.json`，不改 cc-switch。模型密钥留在 cc-switch（本机是 `127.0.0.1:15721`）。
- 不把 API key 写进仓库、命令行回显或事件日志。
