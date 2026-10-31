---
name: claude-code-bridge
description: >-
  在本机启动 Claude Code（claude -p），用 cc-cursor-bridge 插件按 CC_TASK_ID
  读取 hook 事件。先回复 pending，再批量读 push。Stop 用来验收，不满足就 block。
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

`--plugin-dir` 指到含 `.claude-plugin/plugin.json` 和 `claude/hooks/hooks.json` 的检出。本机用 **`~/Sync/Test/cc-cursor-bridge` 的 `main`**，不要写死旧功能分支名（例如曾经的 `feat/plugin-skeleton`）。这台 self-hosted worker 上的 cloud agent 不会从仓库或工作区的 `.cursor/skills/`、`~/.cursor/plugins/local`、`~/.agents/skills/` 加载这份 skill。它只在 Cursor 里做过 Customize → Skills 的 “Sync Skills for Cloud Agents” 之后才能看到。同步会把 `~/.cursor/skills/claude-code-bridge/` 移进 AgentStores；之后改了 skill 要再同步一次。

Prompt 必须走 stdin。用检出里的 `scripts/run_claude_bridged.sh`：它固定 `claude -p`，`--plugin-dir` 指向该检出，prompt 只从 `--prompt-file` 或 stdin 读，`--allowedTools` 排在参数里因此吃不到 prompt。未设 `CC_TASK_ID` 时脚本会生成。标准输出在 `~/cc-$CC_TASK_ID.jsonl`，标准错误在 `~/cc-$CC_TASK_ID.err`。

```bash
export CC_TASK_ID="$(python3 -c 'import uuid; print(uuid.uuid4().hex)')"
export CC_BRIDGE_DIR="${CC_BRIDGE_DIR:-$HOME/.cc-bridge}"
~/Sync/Test/cc-cursor-bridge/scripts/run_claude_bridged.sh \
  --cwd /path/to/repo \
  --prompt-file /tmp/cc-prompt.txt \
  --allowed-tools 'Write,Edit,Read,Glob,Grep'
```

不用脚本时，同样用重定向，不要把任务说明放在 argv 末尾：

```bash
claude -p \
  --plugin-dir ~/Sync/Test/cc-cursor-bridge \
  --output-format stream-json \
  --verbose \
  --include-hook-events \
  --permission-mode acceptEdits \
  --permission-prompts none \
  --allowedTools 'Write,Edit,Read,Glob,Grep' \
  < /tmp/cc-prompt.txt \
  > ~/cc-$CC_TASK_ID.jsonl \
  2> ~/cc-$CC_TASK_ID.err
```

`--allowedTools` 和 `--disallowedTools` 会吃掉后面的参数，直到下一个选项。不要写成 `claude -p --allowedTools "Write,Edit" "任务说明"`，也不要让这类 flag 夹在 `-p` 和 prompt 之间。那样 prompt 到不了 `-p`，stderr 是 `Input must be provided either through stdin or as a prompt argument`。只有 prompt 紧挨在 `-p` 后面、且后面没有 consuming flag 时，才可以用 `claude -p "$(cat prompt.txt)" --plugin-dir …`。启动例子一律走 stdin。`run_claude_bridged.sh --dry-run` 只打印 argv，并注明 stdin，不要求本机有 `claude`。

权限：

- `--permission-mode acceptEdits` 让文件编辑不用逐次确认。
- Bash 不要整类放行。只把这次任务真正要用的命令写进 `--allowedTools`，例如 `Bash(python3 *)`、`Bash(git status *)`、`Bash(git diff *)`。
- 不要用 `--dangerously-skip-permissions`、`--allow-dangerously-skip-permissions`，也不要 `--permission-mode bypassPermissions`。
- `--permission-prompts none` 让没人能回答的权限提示直接被 Claude Code 拒绝，避免 `-p` 卡住。插件的 PreToolUse 仍会先跑。
- 在没有审批界面的 `-p` 会话里，`acceptEdits` 也不一定会放行 Write，或放行会改文件的 Bash。只读命令（例如 `echo hi`）在 `--allowedTools Bash` 下可以执行。普通写入被 Claude Code 自己拒绝时，事件日志里的 PreToolUse 没有 `decision: deny`。这和插件拦截不是一回事。

把 `CC_TASK_ID` 和 `CC_BRIDGE_DIR` 放在同一条命令的环境里。需要边跑边读日志时，把 `claude` 放到后台或另开管道，不要等进程退出才去看事件文件。

## 事件文件怎么读

每一行是一个 JSON 对象，字段：

- `seq`：任务内统一序号。`channel` 是 `wait`、`push`、`log` 或 `off`
- `ts`：本地时间
- `event`：hook 名
- `session_id`
- `summary`：工具名、命令、路径、Edit 的 old/new、Bash 输出，长内容已截断；Stop/SubagentStop 另有回答的短预览
- `decision` / `rule_id` / `decision_reason`：这次有没有被规则或回复决定
- `pending_id`：若正在或已经走过等待通道
- `notify`：`disabled` 表示 notifier 没推；`queued_local` 表示只写了本地 outbox，没有 HTTP
- `last_assistant_message` / `transcript_path`：Stop、SubagentStop 的最终回答和 transcript 路径；SubagentStop 还可能有 `agent_transcript_path`、`agent_type`、`agent_id`
- `tool_response` / `error` / `is_interrupt`：PostToolUse、PostToolUseFailure 的返回或错误，主通知里截断，完整在 `.full.json`
- `assistant_text`：助手文本打开时，MessageDisplay push 里的文本片段，用 `message_id` 归组

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

`rm -r`（没有 `-f`）和 `git push origin +branch` 这种 refspec 强制推送当前不拦。 `.claude-flow/`、`.swarm/` 由默认规则 `agent-artifact-dirs` 拦截（回复不能改写）；PR 前不要 `git add -A` 把它们带进提交。

## 默认分流

`config/bridge.json` 的 `events` 给每个事件一个模式：`wait`、`push`、`log`、`off`。单次覆盖用 `CC_BRIDGE_EVENT_MODE_<事件名>`，例如 `CC_BRIDGE_EVENT_MODE_Stop=log`。

- `wait`（同步，写 pending 并阻塞）：PreToolUse、PermissionRequest、Elicitation、Stop
- `push`（异步，写 `push/<seq>-<事件>.json`，并追加 `push/stream.jsonl`，不阻塞）：SessionStart、UserPromptSubmit、UserPromptExpansion、PostToolUse、PostToolUseFailure、StopFailure、SubagentStop、TaskCreated、TaskCompleted、TeammateIdle、WorktreeCreate、PreCompact、PreModelSwitch
- `log`（异步，只写 `events.jsonl`）：Setup、InstructionsLoaded、MessageDisplay、PermissionDenied、PostToolBatch、Notification、SubagentStart、ConfigChange、CwdChanged、DirectoryAdded、FileChanged、WorktreeRemove、PostCompact、PostModelSwitch、ElicitationResult、SessionEnd
- `off`：尽快返回，不写日志。PreToolUse 命中安全规则时仍然立刻 deny 并记日志

SessionStart 在 hooks 里保持同步，这样 `CC_BRIDGE_WATCH`（路径用 `os.pathsep` 分隔）能注入 `watchPaths`。设了这个变量时，即使模式是 wait，也改成同步返回、不等回复，同时仍写一条 push。没设时 SessionStart 也是立刻返回的 push。

WorktreeCreate 在 hooks 里保持同步：脚本自己建好 git worktree 并把路径打到 stdout，同时 push 一条通知，不等你回复。

调试时可以 `CC_BRIDGE_WAIT_ALL=1`，脚本会对全部 33 个事件走 pending 等待。PUSH/LOG 在 hooks.json 里是 `async: true`，Claude Code 不会为它们阻塞；真正拦住进程的是上面四个 wait，加上 SessionStart 和 WorktreeCreate 这两个同步但不等人的例外。

等待超时（`wait_all.timeout_seconds`，默认 120，可用 `CC_BRIDGE_WAIT_ALL_TIMEOUT` 覆盖）要小于 hook timeout。WAIT 事件的 timeout 是 180 秒。SessionEnd 是 55 秒。失败策略：PreToolUse 和 PermissionRequest 超时 deny，Elicitation 超时 decline，Stop 超时放行结束。

## 助手文本（默认关）

MessageDisplay 自带 `delta`（助手文本片段）、`message_id`、`final`。要把 CC 中间的文本实时带给监督者，设 `CC_BRIDGE_ASSISTANT_TEXT=1`，或把 `config/bridge.json` 的 `assistant_text.enabled` 设为 `true`：插件会把 MessageDisplay 从 log 改成 push，通知里带 `assistant_text`、`message_id`、`turn_id`、`index`、`final`、`transcript_path`。选它而不是按偏移量读 transcript，是因为事件里已经有片段，不用处理文件增长、半行 JSON 和多个异步 hook 并发读取。片段按 `message_id` 归组，`final: true` 表示这条消息结束；某段超过 `log.max_field_chars` 时主通知截断，完整文本在同目录 `.full.json`。开关关闭时 MessageDisplay 仍是 log，行为与之前完全一致。

## 你的循环

`claude -p` 放后台。每一轮先处理会卡住的 pending，再批量读 push：

```bash
python3 ~/Sync/Test/cc-cursor-bridge/scripts/bridge.py pending "$CC_TASK_ID"
python3 ~/Sync/Test/cc-cursor-bridge/scripts/bridge.py reply "$CC_TASK_ID" <seq> '<json>'
python3 ~/Sync/Test/cc-cursor-bridge/scripts/bridge.py push "$CC_TASK_ID"
python3 ~/Sync/Test/cc-cursor-bridge/scripts/bridge.py watch "$CC_TASK_ID"
python3 ~/Sync/Test/cc-cursor-bridge/scripts/bridge.py status "$CC_TASK_ID"
```

`pending` 只列出还没有 `replies/<seq>.json` 的等待项。回复生效或超时作出决定后，`pending/<id>.json` 会删掉；`replies/`、`events.jsonl` 和 `approvals.jsonl` 留下。`approvals.jsonl` 每一行是一次门控决定（规则拒绝、回复、超时），字段有 `ts`、`seq`、`event`、`tool`、`decision`、`via`、`reason`，规则拒绝还有 `rule_id`。`push` 列出尚未消费的推送并前进游标；`--since <seq>` 从该序号之后再列一次。`pending` 和 `push` 每行第一列是完整路径，没有 kind 前缀；有预览时在路径后接一个 TAB 和 `# reply: ...` / `# assistant: ...`，没有预览时只有路径，和以前一致。`watch` 把新出现的 pending 和 push 各打一行（`pending <路径>` 或 `push <路径>`），预览同样用 TAB 接在路径后，适合盯着看。预览格式：`# reply: <回答第一行>`（Stop/SubagentStop）、`# response: ...`（PostToolUse）、`# error: ...`（PostToolUseFailure）、`# assistant: ...`（助手文本打开时）。加 `--once` 只打当前这一批。

回复是一个 JSON 对象：

- PreToolUse：`{"decision":"allow|deny|ask|defer","reason":"...","updatedInput":{...}}`
- PermissionRequest：`{"decision":"allow|deny","message":"...","updatedInput":{...}}`
- Elicitation：`{"decision":"accept|decline"}`
- Stop：这是验收。测试或要求还没满足时 `{"decision":"block","reason":"具体缺什么"}`，让 Claude Code 继续做；满足了就 `{"ack":true}` 结束。通知里的 `last_assistant_message` 是 CC 的最后一条回答，验收前先看它。`stop_hook_active` 为 true 时，没有明确的 block 就让它停。每个任务最多阻断 `stop.max_blocks` 次（默认 3），到了上限即使再 block 也放行结束。**外部依赖这次会话解不了时**（例如 CI runner 离线、公平队列被压测占满）：应 `{"ack":true}`，把缺口写进 PR/报告；不要用同一理由反复 block Stop，也不要抬高 `CC_BRIDGE_STOP_MAX_BLOCKS` 空转
- 其余事件在 `wait_all` 里才会要回复：可阻断的用 `{"decision":"block","reason":"..."}`，否则 `{"ack":true}`

命令和文件内容超过上限时（默认命令 4000 字、文件内容 2000 字），短文件里该字段是一个带 `text`、`truncated: true`、`original_length` 和 `sha256` 的对象，完整且已脱敏的正文在同目录的 `<seq>-<事件>.full.json`。Stop/SubagentStop 的 `last_assistant_message`、PostToolUse 的 `tool_response`、PostToolUseFailure 的 `error`、开关打开时的 `assistant_text` 也走同一套；其中工具返回和错误在主通知里按 `log.max_field_chars`（默认 240）截断。命令或回答被截断时，先读这份 `.full.json` 再决定 allow 或验收。

PreToolUse 回 `allow` 会让这次工具调用跳过权限提示。只在你看过完整命令和路径之后才 allow。

`auto_allow.cwd_read` 默认关。只有 `CC_BRIDGE_AUTO_ALLOW_CWD_READ=1`（或配置里打开）时，落在会话 cwd 内的 `Read` 才不写 pending、直接 allow。`.env`、`~/.ssh`、`.claude-flow/` 仍然先被规则拒绝。cwd 外和指到 cwd 外的符号链接仍然要等你回复。不要假设 Read 会自己放行。

旧的窄等待仍然可用，但只在该事件的模式是 `log` 时生效：`CC_BRIDGE_WAIT=1` 等 PermissionRequest，`CC_BRIDGE_WAIT_PRE_TOOL=1` 再加 PreToolUse。旧目录里文件名是 `pending/<id>.json`，不是 seq；AHP 目录里它是普通 gate（`pending/<seq>-<事件>.json`，可用 `--ahp` 回复）。回复不合法时立即 deny。

notifier 默认关，而且当前版本即使打开也只把打算发给 Cursor Cloud Agents API 的 followup 写到 `outbox/`，不发 HTTP。API key 只从环境变量 `CC_BRIDGE_CURSOR_API_KEY` 读取，禁止写进仓库、命令行回显或事件日志。

## 怎么判断结束

有序：**先读终端态文件，再看进程，最后看事件日志。**

1. `python3 ~/Sync/Test/cc-cursor-bridge/scripts/bridge.py status "$CC_TASK_ID"`（AHP 目录加 `--ahp`），或直接读 `$CC_BRIDGE_DIR/tasks/$CC_TASK_ID/status.json`。`SessionEnd` 时写入，`state` 固定 `ended`；同时有 `ended_at`、`session_id`、`session_end_reason`、`stop_accepted`（`true` 已验收 / `false` 未验收 / `null` 没走过 Stop，例如 `max_turns`）、`stop_seq`。没有这个文件时命令打一行 `missing: <路径>`。
2. `claude` 进程退出码。
3. `stream-json` 里 `"type":"result"` 的那一行（成功时 `subtype` 一般为 `success`，失败时 `is_error` 为 true）。
4. 事件日志里的 `Stop`。你 ack 之后进程才会真正结束；block 会让它再做一轮。`SessionEnd` 是异步日志，可能稍晚出现，所以 step 1 只在它写完之后才有效。

没写 pending 回复之前，进程会停在 PreToolUse、PermissionRequest、Elicitation 或 Stop 上。push 和 log 不挡进程。

### 复活 / 续写硬规则

父会话被压缩后继续、或监督进程重启后接手一个已知的 `CC_TASK_ID` 时，**第一件事是 `bridge.py status <task>`**（或直接读那个文件），然后才说话。

- 已经 `state: ended`：对用户的第一句话必须是终端结果——`stop_accepted` 的含义、`session_end_reason`、`ended_at`，再补文件 / 测试 / cost。
- **禁止**在看到 `ended` 之后还先发「正在修…」「还在跑」「稍等，我继续」这类过期状态。进程已经退出，`pending` 里没有等你回复的项；把它们当成新任务的前提就错了。
- 要接着做，就换一个新的 `CC_TASK_ID` 重开一次；不要往已结束的旧任务目录里续写。
- 只有 `missing:` 才表示还没有终端证据：这时再看进程是否还活着，然后照常走 pending / push 循环。

## AHP v1（0.2.0 起）

- 旧目录、旧命令、旧回复写法都照旧可用，你的循环不用改。
- 同一批事件也写在 `$CC_BRIDGE_DIR/ahp/tasks/$CC_TASK_ID/`（AHP v1）。命令加 `--ahp` 就读写那边：`pending --ahp`、`reply ... --ahp`、`push --ahp`、`watch --ahp`。旧目录的 seq 可能有空号（AHP 独有的 `policy.deny`、`producer.error`、`plan.update` 也占号）。
- 每个等待项结束后，`ahp/tasks/<task>/acks/<seq>.json` 写明实际执行了什么（`status`、`via`、`applied_decision`，Stop 还有 `accepted`）。不确定回复有没有生效，就看 ack。
- 回复不合法时按默认决定处理，不会放行：PreToolUse/PermissionRequest deny，Elicitation decline，Stop 结束本轮。`reply` 会先校验，不合法 exit 2、什么都不写；看 stderr 改了再写。Stop 的 `block` 一定要带 `reason`。
- `updatedInput` 改写的命令会重跑危险规则，命中仍然 deny。
- `additionalContext` 只在等待项（pending）的回复里有效，最长 10,000 字符；超了整条回复作废（不截断），按默认决定处理。对 push 通知写回复没有用。Stop 的回复不能带 `additionalContext`。
- hook 自己出错也按默认决定处理（fail-closed）。看 `ahp/.../push/*-producer.error.json`。

## 出错怎么办

- 任务目录或 `events.jsonl` 一直没有：`--plugin-dir` 没指到插件根，或 hook 找不到 `python3`。用 `--debug-file <日志> --debug=hooks` 重跑一次，看 hook 的 stderr。
- stderr 是 `Input must be provided either through stdin or as a prompt argument`：prompt 被 `--allowedTools`（或别的 consuming flag）吃掉了，没有到达 `-p`。把 prompt 放进文件，用 `run_claude_bridged.sh --prompt-file` 或 `claude -p … < prompt.txt` 重跑，并换一个新的 `CC_TASK_ID`。
- 流里出现 hook error：分发脚本把非 JSON 打到了 stdout，或脚本崩了。事件日志的 `summary.config_note` 会写配置回退。
- `StopFailure`：这一轮是 API 错误。看 `summary.error`，不要改用户的代理配置。
- pending 超时：插件已经按默认 deny。要重试就再跑一轮，或在超时前写入 reply。
- 回复写了但被当成无效：看 `ahp/tasks/<task>/acks/<seq>.json` 的 `errors`。常见原因：拼错字段、`decision` 不在允许范围、Stop `block` 没有 `reason`。
- 工作区不是 git 仓库时，`WorktreeCreate` 会失败。这个 hook 替换了 Claude Code 自带的 worktree 行为，脚本自己执行 `git worktree add`，分支名是 `cc-bridge/<slug>`。
