# Agent Hook Protocol v1（AHP v1，`ahp/1`）

- 状态：**草案 1.0.0-draft.3**（2026-10-22），分支 `feat/ahp-v1-spec`，基线 cc-cursor-bridge `9f478f8`
- draft.2 变更：吸收 RINO 开发 P0 对表的 8 项共识；draft.3：注入上下文（§7.3）与逐事件 fail-closed（§9.1），见文末「变更记录」
- 一致性测试：`python3 -m conformance`（见 `conformance/README.md`）
- 机器可读部分：`spec/schemas/*.schema.json`（JSON Schema 2020-12）、`spec/examples/`
- 校验：`python3 spec/tools/validate.py`；旧格式转换：`python3 spec/tools/legacy_to_ahp.py <task-dir>`

本文的关键词 **MUST / MUST NOT / SHOULD / SHOULD NOT / MAY** 按 RFC 2119 / RFC 8174 解释，只在大写时有规范效力。其余正文用中文。

---

## 1. 范围

AHP 规定一个**智能体运行时**（producer）怎样把自己的生命周期事件交给一个**监督者**（consumer），以及监督者怎样对其中需要决定的事件作答。它只规定文件、字段和时序，不规定传输以外的任何进程模型、不规定模型或工具本身。

AHP v1 来自 cc-cursor-bridge 在 Claude Code 上的实际用法（33 个 hook、4 个默认等待事件、pending/reply 文件通道）。v1 的目标：

1. 同一个监督者（RINO、无事APP、Ruhomo 等 bot）既能监督 Claude Code（经适配层），也能监督 RINO 自己的运行时，**不改监督逻辑**。
2. 审批默认**出错即拒绝（fail-closed）**。
3. 本地文件系统可用；沙箱（CubeSandbox / E2B）传输作为第 14 节的扩展，Phase 3 再定稿。

不在 v1 范围：网络推送（notifier）、多 consumer 仲裁、跨任务的全局策略。

## 2. 版本

2.1 协议标识是 `ahp/<major>`。本文定义 `ahp/1`。每个 envelope、reply（SHOULD）、ack、`capabilities.json`、`consumer.json` 都带 `protocol` 字段。

2.2 `capabilities.json` 里的 `spec_version` 是 `1.<minor>.<patch>`，草案期可带预发布后缀 `-draft.<N>`（语义化版本的 pre-release，完整形式 `^1\.\d+\.\d+(-draft\.[1-9]\d*)?$`）。草案期 producer SHOULD 写出它实现的确切草案（例如 `1.0.0-draft.3`）。consumer 判断兼容时 MUST 忽略预发布后缀、只按下面的 major/minor/patch 规则比较；后缀与自己依据的草案不同时 MAY 发出警告，但 MUST NOT 仅因此拒绝任务。`consumer.json` MAY 带同格式的 `spec_version`，说明 consumer 依据的版本，仅供参考。


- **patch**：只改文字、示例、schema 的描述。不改变任何合法实例的合法性。
- **minor**：只做加法——新的可选字段、新的 notify/log 事件、新的可选 feature 开关、新的 `x-` 扩展。旧 consumer MUST 能继续工作。
- **major**（`ahp/2`）：删除或改名字段、改变 gate 事件集合或其默认决定、改变目录布局、改变失败语义。

2.3 兼容规则：

- consumer MUST 忽略不认识的字段、不认识的 notify/log 事件（它们可能来自更新的 minor 版本）。schema 的严格性用于一致性测试和 producer 自检；consumer MUST NOT 因为 notify/log 事件不过 schema 就中断任务。
- consumer 遇到不认识的 **gate** 事件 SHOULD 回复 envelope 里的 `default_decision`，或不回复让超时生效；除非 `default_decision` 本身是 `ack`，MUST NOT 回复 allow / accept / ack 这类放行决定。
- reply 的 schema 禁止未知字段（`additionalProperties: false`），扩展字段 MUST 以 `x-` 开头。producer 对含未知非 `x-` 字段的 reply MUST 按 invalid 处理（§9.4）。理由：审批答复里的拼写错误（如 `updatedinput`）被静默丢掉是安全问题。
- envelope、capabilities、consumer.json 同样只允许已定义字段和 `x-` 扩展；新增字段只能随 minor 版本进入 schema。
- 字段名 MUST 是 `snake_case`。事件名 MUST 匹配 `^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$`。厂商私有事件 MUST 用 `x-<vendor>.` 前缀（例如 `x-rino.goal.change`、`x-cc.setup`），consumer 按 notify/log 的兼容规则对待。

## 3. 角色与能力协商

3.1 **producer**：产生事件并执行决定的一方。例：Claude Code 适配层（`cc-cursor-bridge` 的 hook 脚本）、RINO 运行时。每个任务目录 MUST 只有一个 producer 实例写入（多进程 producer 共享同一把锁，见 §6）。

3.2 **consumer**：读取事件、对 gate 作答的一方，例如驱动任务的 bot。一个任务 SHOULD 只有一个 consumer 写 `replies/`。多个只读观察者 MAY 同时读。

3.3 协商流程：

1. consumer MAY 在启动 producer **之前**创建任务目录并写 `consumer.json`（schema：`consumer.schema.json`）。内容：接受的协议、想把哪些事件升/降级到哪个类（`event_classes`）、各 gate 超时（`timeouts_ms`）、要打开的可选 feature、追加的 deny 规则（`extra_rules`）。
2. producer 在任务开始、写出第一个事件**之前** MUST 原子写出 `capabilities.json`（schema：`capabilities.schema.json`），写入后不再修改。它列出：
   - 每个事件的**生效**类（`class`）、允许的类、gate 的 `decisions`、`default_decision`、`timeout_ms`；
   - feature 开关（`acks`、`full_files`、`updated_input`、`additional_context`、`message_delta`、`policy_deny_event`、`consumer_rules`）；
   - 截断上限、`max_blocks`、注入上下文上限 `max_additional_context_chars`（§7.3）；宿主 hook 超时 `host_hook_timeout_ms`（有宿主超时时 MUST 给出，进程内 producer 如 RINO 可以不填），以及 producer 自己允许的最长 gate 等待 `max_gate_timeout_ms`（不填宿主超时时 MUST 给出）；
   - 静态规则的 id 列表、规则文件的 sha256、每条规则的 `status`（`applied | partially_applied | unapplied`）和 `required` 列表（§11）；
   - `consumer_json`：`absent | applied | partially_applied | rejected`，以及 `consumer_notes` 说明没采纳的项。
3. producer 对 `consumer.json` 的处理：
   - MUST 只采纳 `allowed_classes` 内的类变更；
   - MUST 把 `timeouts_ms` 夹在 `[1000, 上限]`：有 `host_hook_timeout_ms` 时上限是 `host_hook_timeout_ms − 10000`，没有时是 `limits.max_gate_timeout_ms`（§9.2）。任何一项被夹过，`consumer_json` MUST 是 `partially_applied`，并在 `consumer_notes` 写明原值和夹后的值；
   - `extra_rules` MUST 只能增加拒绝，MUST NOT 关闭或放宽内置规则；producer 不能完整执行的规则 MUST 在 `rules.status` 里标成 `partially_applied` 或 `unapplied`，MUST NOT 静默跳过；
   - `consumer.json` 无法解析时 MUST 当作 `rejected`，按 producer 默认运行，并在 `consumer_notes` 里写明。
4. consumer 读到 `capabilities.json` 后 MUST 检查 `protocol`。不支持时 consumer MUST NOT 回复任何 gate（让 producer 超时 → fail-closed），并 SHOULD 终止该任务。
5. 标了 `required: true` 的规则如果不是 `applied`，consumer SHOULD 拒绝开始（终止任务、不回复任何 gate）。

## 4. 事件模型

### 4.1 三个类

| 类 | producer 行为 | consumer 行为 |
| --- | --- | --- |
| **gate** | 写 `pending/<seq>-<event>.json`，阻塞直到收到合法 reply 或到达 `deadline`；然后写 `acks/<seq>.json`；把结果记进 `events.jsonl` | MUST 在 deadline 前写 `replies/<seq>.json` |
| **notify** | 写 `push/<seq>-<event>.json`，追加 `push/stream.jsonl`，不阻塞 | 读，不回复 |
| **log** | 只追加 `events.jsonl` | 可读，不回复 |

不论类别，每个事件 MUST 在 `events.jsonl` 里有且只有一条记录（带 `outcome`，见 §5.3），作为审计日志。`off` 不是协议里的类：producer MAY 不发某个事件，但 MUST 在 `capabilities.json` 里不列出它。

### 4.2 事件表

「默认类」是 producer 没收到 `consumer.json` 时的类。「可升级」表示 consumer 可把它升为 gate。CC 对应关系的细节在附录 A。

| AHP 事件 | 默认类 | 可升级为 gate | 决定集合（作为 gate 时） | 超时默认决定 | CC hook | 说明 |
| --- | --- | --- | --- | --- | --- | --- |
| `tool.pre` | **gate** | — | allow / deny / ask | deny | PreToolUse | 工具执行前的主门。命中静态规则时不进 gate，见 §11 |
| `permission.request` | **gate** | — | allow / deny | deny | PermissionRequest | 运行时自己的权限层发问（CC 的 safetyCheck 只有这里能放行）。只有一层权限的运行时 MAY 不实现 |
| `input.request` | **gate** | — | accept / decline | decline | Elicitation | 结构化提问（MCP elicitation）；accept 时带 `content` |
| `turn.stop` | **gate** | — | ack / block | ack（结束，未验收） | Stop | 验收门；block 让智能体继续，见 §9.3 |
| `session.start` | notify | 是 | ack | ack | SessionStart | 可回 `additional_context`、`watch_paths` |
| `session.end` | log | 否 | — | — | SessionEnd | 进程退出中，不得等待 |
| `prompt.submit` | notify | 是 | ack / block | block | UserPromptSubmit | |
| `prompt.expand` | notify | 是 | ack / block | block | UserPromptExpansion | 斜杠命令展开 |
| `message.delta` | log | 否 | — | — | MessageDisplay | 助手文本片段；按 `message_id` 聚合，`final` 结束；可选 `kind: text\|reasoning`（§4.3） |
| `plan.update` | notify | 否 | — | — | 由 PostToolUse(TodoWrite) 合成（附录 A.1） | 计划 / todo 列表变化，payload `items: [{content, status}]`，每次给全量 |
| `tool.post` | notify | 是 | ack / block | ack | PostToolUse | 带 `tool_response` |
| `tool.error` | notify | 是 | ack | ack | PostToolUseFailure | 带 `error`、`is_interrupt`。运行时把审批中断作为 `permission.request` 发出时，MAY 不再为它发 `tool.error` |
| `tool.batch` | log | 否 | — | — | PostToolBatch | |
| `permission.denied` | log | 否 | — | — | PermissionDenied | 拒绝已发生 |
| `subagent.start` | log | 否 | — | — | SubagentStart | |
| `subagent.stop` | notify | 是 | ack / block | ack | SubagentStop | 带子代理最后回答 |
| `task.create` | notify | 是 | ack / block | block | TaskCreated | |
| `task.complete` | notify | 是 | ack / block | ack | TaskCompleted | |
| `teammate.idle` | notify | 是 | ack / block | ack | TeammateIdle | |
| `turn.error` | notify | 否 | — | — | StopFailure | API 失败，本轮已结束 |
| `compact.pre` / `compact.post` | notify / log | 是 / 否 | ack / block | ack | PreCompact / PostCompact | |
| `model.switch.pre` / `.post` | notify / log | 是 / 否 | allow / deny / ask | deny | PreModelSwitch / PostModelSwitch | |
| `worktree.create` | notify | 是 | ack | ack | WorktreeCreate | producer 自己建好 worktree；consumer 只能 ack 或给 `worktree_path` |
| `worktree.remove` | log | 否 | — | — | WorktreeRemove | |
| `input.result` | log | 否 | — | — | ElicitationResult | |
| `agent.notification` | log | 否 | — | — | Notification | |
| `session.setup`、`instructions.load`、`config.change`、`cwd.change`、`directory.add`、`file.change` | log | 否 | — | — | Setup、InstructionsLoaded、ConfigChange、CwdChanged、DirectoryAdded、FileChanged | |
| `policy.deny` | notify | 否 | — | — | （新增，无 CC 对应） | 静态规则直接拒绝了某个 gate，见 §11 |
| `producer.error` | notify | 否 | — | — | （新增） | producer 内部失败及其采用的默认决定，见 §9 |

v1 的**必需 gate 集合**是 `tool.pre`、`turn.stop`。`permission.request`、`input.request` 在运行时有对应能力时 MUST 作为 gate 实现，否则 MUST NOT 出现在 `capabilities.json`。

### 4.3 payload

每个事件的 payload 字段见 `spec/schemas/events/<event>.schema.json`。没有专门 schema 的事件，payload 是任意对象，字段 SHOULD 用宿主原字段的 snake_case 形式。共同约定：

- 工具类事件：`tool_name`、`tool_use_id`、`tool_input`、`mcp_server`。
- 回答类事件：`last_assistant_message`（`turn.stop`、`subagent.stop`）。
- 任何字符串字段都 MAY 被截断，截断后是截断对象（§8）。
- 会话上下文（`session_id`、`cwd`、`transcript_path`、`prompt_id`、`permission_mode`、`turn_id`、`agent_id`）放在 envelope 的 `context`，不放 payload。
- **子代理**：子代理的事件 MUST 用父任务的 `task_id`（同一个目录、同一条 seq），以 `context.agent_id` 区分；主代理的事件不带 `agent_id`。
- **`tool.pre` 的规范化视图**（为了规则可移植）：payload MAY 带 `tool_kind`，取值 `shell | edit | read | search | fetch | ask | other`。`tool_kind = shell` 时 producer SHOULD 带 `command`：把 argv 按 POSIX shell 规则逐个 quote 后用空格拼接（Python `shlex.join` 的结果），供 `command_regex` 匹配。宿主本来就是命令字符串时（CC 的 Bash），`command` 就是那个字符串。
- **`message.delta`**：`message_id` 是不透明字符串，consumer MUST NOT 解析；`index` 在同一个 `message_id` 内从 0 开始、连续无空洞；该消息的最后一个片段带 `final: true`。可选 `kind`：`text`（缺省）或 `reasoning`；producer 对 `reasoning` 片段 SHOULD 默认不发，需要时由 `consumer.json.features.reasoning_delta` 打开。
- **`plan.update`**：`items` 是完整列表（不是增量），每项 `{content, status}`，`status` 取 `pending | in_progress | completed | cancelled`，MAY 带 `id`。

### 4.4 gate 的实现方式

gate MAY 由运行时以「中断本回合 → 外部裁决 → 恢复」的方式实现（例如 RINO 的 `approval/requested` → `decide_approval` → resume），不要求在同一个调用栈里阻塞，前提是：

- 工具在收到 allow 之前 MUST NOT 执行；
- `deadline`、`default_decision`、ack 和 fail-closed 语义与原地阻塞完全一致——中断期间到期一样按 `expired` 处理；
- 进程在中断期间退出时，producer MUST 在退出前对未决 gate 写 `expired` ack 并采用默认决定（不能留下无 ack 的 pending）。

## 5. Envelope

### 5.1 字段

```json
{
  "protocol": "ahp/1",
  "task_id": "demo-bridge-snapshot",
  "seq": 4,
  "event": "tool.pre",
  "class": "gate",
  "producer": {"name": "cc-cursor-bridge", "version": "…", "host": {"name": "claude-code", "version": "2.1.306"}},
  "ts": "2026-10-22T01:27:05.487+08:00",
  "payload": {"tool_name": "Skill", "tool_input": {"skill": "…"}},
  "context": {"session_id": "…", "cwd": "/private/tmp/ccb-demo2-woqt"},
  "reply_file": "replies/4.json",
  "deadline": "2026-10-22T01:29:05.487+08:00",
  "timeout_ms": 120000,
  "decisions": ["allow", "deny", "ask"],
  "default_decision": "deny",
  "native": {"event": "PreToolUse"}
}
```

| 字段 | 必需 | 说明 |
| --- | --- | --- |
| `protocol` | MUST | `"ahp/1"` |
| `task_id` | MUST | 匹配 `^[A-Za-z0-9._-]{1,200}$`，且不是 `.`/`..` |
| `seq` | MUST | 任务内严格递增的正整数（§6.3） |
| `event` | MUST | §4.2 的名字或 `x-` 扩展 |
| `class` | MUST | 本次**生效**的类 |
| `producer` | MUST | `{name, version, host?}` |
| `ts` | MUST | RFC 3339，MUST 带显式偏移（`Z` 或 `±hh:mm`），SHOULD 精确到毫秒 |
| `payload` | MUST | 对象 |
| `reply_file` | gate 时 MUST | 相对任务目录的路径，固定为 `replies/<seq>.json` |
| `deadline` | gate 时 MUST | RFC 3339 绝对时间；过了这个时间 producer 不再等 |
| `timeout_ms`、`decisions`、`default_decision` | gate 时 SHOULD | 让 consumer 不查 capabilities 也能作答 |
| `context` | SHOULD | 会话上下文 |
| `truncated`、`full_file` | 截断时 MUST | 见 §8 |
| `native` | MAY | 宿主原始事件名和键名，仅供排错；consumer MUST NOT 依赖它做决定 |
| `outcome` | 仅 `events.jsonl` | 见 §5.3 |
| `x-*` | MAY | 扩展 |

envelope 不允许其他顶层字段。

### 5.2 文件名

- `pending/<seq>-<event>.json`、`push/<seq>-<event>.json`，`<event>` 是 AHP 事件名（含点），例如 `pending/4-tool.pre.json`。
- 截断时的完整版本：同目录 `<seq>-<event>.full.json`，内容是同一个 envelope，所有字段不截断（仍脱敏），`truncated: false`。
- consumer MUST 以文件内容里的 `seq` 和 `event` 为准，文件名只用于排序和查找。

### 5.3 events.jsonl 审计记录

每行一个 envelope，额外带：

```json
"outcome": {"decision": "deny", "via": "reply|timeout|rule|error|invalid_reply|none", "reason": "…", "rule_id": "…"}
```

- `ts` 是记录时间（gate 是作答完成的时间）。
- payload SHOULD 按 `limits.max_field_chars` 截断，审计日志不追求完整。
- 带 `outcome` 的 gate 记录不要求 `reply_file`/`deadline`（静态规则直接拒绝的 tool.pre 没有 pending）。

## 6. 目录布局与写入规则

### 6.1 布局

```text
<root>/tasks/<task_id>/
  capabilities.json        producer，任务开始写一次
  consumer.json            consumer，可选，producer 启动前写
  seq                      producer，计数器 + 锁
  events.jsonl             producer，审计日志（追加）
  pending/<seq>-<event>.json [+ .full.json]   producer
  push/<seq>-<event>.json    [+ .full.json]   producer
  push/stream.jsonl        producer，notify 事件的追加流
  replies/<seq>.json       consumer
  acks/<seq>.json          producer
  state/                   producer 私有（例如 stop 阻断计数）
  consumer/                consumer 私有（例如 push 游标）
```

`<root>` 在本地实现里默认 `~/.cc-bridge`（CC 适配层沿用 `CC_BRIDGE_DIR`）；协议只要求 producer 和 consumer 对同一个 `<root>` 达成一致。

### 6.2 原子写与单写者

- 每个文件 MUST 只有一个写者角色（见上表）。consumer MUST NOT 写 producer 的文件，反之亦然。
- 除追加型文件（`events.jsonl`、`push/stream.jsonl`）外，所有文件 MUST 以「同目录临时文件 + rename」方式原子写入。临时文件名 SHOULD 以 `.` 开头并以 `.tmp` 结尾（例：`.4.json.<pid>.<rand>.tmp`）；读者 MUST 忽略以 `.` 开头或以 `.tmp` 结尾的文件。写者 SHOULD 在 rename 前 fsync 文件。
- 追加型文件 MUST 在排他锁（`flock`/等价机制）下追加完整的一行（含 `\n`）。读者 MUST 容忍最后一行不完整（跳过并稍后重读）。
- `replies/<seq>.json` 写出后 consumer MUST NOT 覆盖它。改主意不被支持：第一个合法 reply 生效。
- 文件 MUST 是 UTF-8 JSON；producer 写 pending/push SHOULD 用缩进格式，追加型文件 MUST 一行一个对象。

### 6.3 seq

- producer MUST 在 `seq` 文件的排他锁内读取、加一、写回、fsync，再释放锁；拿到的值就是该事件的 seq。
- seq 从 1 开始、严格递增；MAY 有空洞（例如保留后没有发出），consumer MUST NOT 假定连续。
- 同一任务的多个 producer 进程（例如 CC 的每个 hook 是独立进程，子代理并发）MUST 共用这把锁。

### 6.4 目录权限

producer 创建任务目录时 SHOULD 用 `0700`，文件 `0600`。

## 7. Reply

### 7.1 Schema

`replies/<seq>.json`（`reply.schema.json`）：

| 字段 | 说明 |
| --- | --- |
| `decision` | MUST。统一取值 `allow / deny / ask / block / accept / decline / ack`，且 MUST 在该 envelope 的 `decisions` 内 |
| `reason` | 给运行时（和人）看的理由。`decision = block` 时 MUST 非空——它就是让智能体继续做的指示 |
| `updated_input` | `tool.pre` / `permission.request` 且 `allow` 时可改写工具输入。producer 不支持时（`features.updated_input = false`）MUST 视为 invalid。改写后 producer MUST 先对新输入**重跑静态规则**（命中 → deny，ack `via: rule`，发 `policy.deny`），再走剩余的 gate（例如 RINO 的 ApprovalGate），并在 `events.jsonl` 的 outcome 里记下实际执行的是改写后的输入。reply 不能改工具名：任何试图改 `tool_name` 的 reply（包括 `updated_input` 里带 `tool_name`）MUST 视为 invalid |
| `additional_context` | 注入给智能体的额外上下文。只在 gate 的 reply 里、且只在 §7.2 列出它的事件上有效；长度上限 `limits.max_additional_context_chars`，见 §7.3 |
| `content` | `input.request` 且 `accept` 时的答案对象 |
| `watch_paths`、`worktree_path`、`display_content` | 对应事件的专用字段 |
| `protocol`、`seq` | SHOULD。若给出 `seq` 且与文件名不符，MUST 视为 invalid |
| `consumer`、`ts` | MAY |
| `x-*` | 扩展 |

### 7.2 各 gate 的合法决定

| 事件 | 合法 decision | 可带字段 |
| --- | --- | --- |
| `tool.pre` | allow / deny / ask | reason、updated_input（仅 allow）、additional_context |
| `permission.request` | allow / deny | reason、updated_input（仅 allow） |
| `input.request` | accept / decline | content（仅 accept） |
| `turn.stop`、`subagent.stop` | ack / block | reason（block 必需） |
| `prompt.submit`、`prompt.expand`、`task.*`、`teammate.idle`、`compact.pre`、`tool.post` | ack / block | reason、additional_context |
| `model.switch.pre` | allow / deny / ask | reason |
| `session.start` | ack | additional_context、watch_paths |
| `worktree.create` | ack | worktree_path |

`ask` 的含义是「交还给运行时自己的交互式确认」。无头运行时（`-p`、沙箱）里 `ask` 等于拒绝，producer SHOULD 在 `capabilities.json` 里对无头模式去掉 `ask`。

reply 带了该事件「可带字段」以外的字段（例如 `turn.stop` 带 `additional_context`）MUST 视为 invalid。

### 7.3 注入上下文（`additional_context`）

1. **只认 gate 的 reply。** producer MUST 只从 gate 事件的合法 reply 里取 `additional_context` 并注入。notify / log 事件不等待回复；consumer 若仍为它写了 `replies/<seq>.json`（例如想在 notify 类的 `session.start` 上注入），producer MUST NOT 注入，并 MUST 在它处理下一个事件之前、或任务结束时（以先到者为准）为该 seq 写 `acks/<seq>.json`：`status: invalid`、`via: invalid_reply`、`applied_decision: ack`、`errors` 说明「不是 gate」。producer 已退出后才出现的这种 reply 没有 ack，consumer MUST 认为它没有生效。想注入就把事件升为 gate（`consumer.json.event_classes`）。
2. **长度上限，不截断。** `capabilities.json` 的 `limits.max_additional_context_chars` 给出单条 `additional_context` 的上限（Unicode 码点，缺省 10000）。超出 MUST 视为 invalid reply（ack `status: invalid`，采用 `default_decision`），MUST NOT 截断后再注入——被截的上下文会让智能体以为自己拿到了全部。宿主有自己的上限、且计数方式不同时，producer MUST 把声明值取得足够小，使任何被接受的字符串都能原样到达智能体（CC 见附录 A.2）。consumer 有更多内容时 SHOULD 自己拆分或改为让智能体按需读取文件。
3. **ack 在注入之前写出**（§9.4 的特例）：producer MUST 先写 `acks/<seq>.json`（`status: consumed`），再把上下文交给运行时。注入失败时不改写 ack，SHOULD 发 `producer.error`。
4. **持久化（说明性）。** 注入的上下文 SHOULD 作为可回放的会话消息保存（例如 CC 把它作为 system reminder 插在 hook 触发处的对话里；RINO 的 `Plugin{form=hook}` 设计同样写成会话消息），而不是改写系统提示。这样恢复会话、重放转录时智能体看到的内容一致，审计时也能在转录里找到它。

## 8. 截断对象

任何字符串字段超过上限时，producer MUST 把它换成：

```json
{"text": "<前缀>", "truncated": true, "original_length": 626, "sha256": "<64 hex>", "full_file": "push/12-tool.post.full.json"}
```

- `text`：脱敏后全文的前缀。MAY 以 `…[truncated]` 结尾；consumer MUST 以 `truncated` 判断，不得解析这个标记。
- `original_length`：脱敏后全文的 Unicode 码点数。
- `sha256`：脱敏后全文 UTF-8 字节的 SHA-256（小写十六进制）。consumer SHOULD 用它校验 `full_file` 里的对应字段。
- `full_file`：相对任务目录的路径。envelope 只要有一个字段被截断，顶层 MUST 有 `truncated: true` 和 `full_file`。
- 非字符串值（例如 `tool_response` 是对象）被截断时，`text` 是其 JSON 序列化的前缀，`full_file` 里保留原始结构。
- 上限：`limits.command_chars`（命令，默认 4000）、`limits.content_chars`（文件内容、最后回答，默认 2000）、`limits.max_field_chars`（工具返回、错误、助手片段、审计日志，默认 240）。
- 对 gate 事件，consumer SHOULD 在 `truncated: true` 时先读 `full_file` 再作答；读不到或 sha256 不符时 SHOULD 拒绝。

## 9. 超时与 fail-closed

### 9.1 硬性要求

**producer 在处理 gate 事件的任何环节出错时，MUST 采用该事件的 `default_decision`，并且这个默认决定 MUST 是拒绝方向的（deny / decline / block，`turn.stop` 见 §9.3）。** 「出错」至少包括：

- 宿主输入无法解析或不是对象；
- producer 自身异常（脚本崩溃、导入失败）；
- 配置或规则文件读不了（MUST 回退到内置规则，并发 `producer.error`）；
- 写不了 pending 或 seq；
- reply 读不了、不是 JSON、不合 schema、决定不在 `decisions` 内（→ §9.4 invalid）；
- 到达 deadline。

producer MUST NOT 因为自身失败而让宿主走「无答复」路径，只要那条路径可能放行。对每次 fail-closed，producer MUST 写 `acks/<seq>.json`（`via: error|timeout|invalid_reply`），SHOULD 发一条 `producer.error` notify。

逐事件的含义（出错、超时、reply 无效三种情形相同，ack 的 `status` 分别是 `expired`（`via: error|timeout`）和 `invalid`）：

| gate | 默认决定 | 宿主上的效果 |
| --- | --- | --- |
| `tool.pre`、`permission.request`、`model.switch.pre` | deny | 工具 / 切换不执行 |
| `input.request` | decline | 拒绝回答提问 |
| `turn.stop` | ack | 结束本轮，`accepted: false`（§9.3） |
| `session.start` | ack | 会话照常开始，**不注入**任何 `additional_context`、`watch_paths` |
| `prompt.submit`、`prompt.expand`、`task.create` | block | 不开启这一轮（提示不交给智能体）；`reason` 说明原因，producer SHOULD 把它带进最终结果（CC 把 block 的 reason 显示给用户；无头模式下进入结果行） |
| 其他升为 gate、默认 `ack` 的事件 | ack | 不采纳 reply 的任何字段（不注入、不改路径） |

默认是 `ack` 的 gate 并不违反上面的「拒绝方向」：它们出错时的安全方向是「consumer 的回复不产生任何效果」，producer MUST NOT 用到任何来自无效或缺失 reply 的字段。

### 9.2 超时默认值

| gate | 默认 timeout_ms | 超时决定 |
| --- | --- | --- |
| `tool.pre` | 120000 | deny |
| `permission.request` | 120000 | deny |
| `input.request` | 120000 | decline |
| `turn.stop` | 120000 | ack（结束，`accepted: false`） |
| 升级为 gate 的其他事件 | 120000 | 见 §4.2 |

约束：

- 有宿主 hook 超时时，`deadline` MUST 早于它至少 10 秒（CC 现为 180 秒 → 最多 170 秒）。原因：宿主超时会把 hook 当作非阻塞错误继续执行，是 fail-open。
- 没有宿主超时的进程内 producer（RINO），不填 `host_hook_timeout_ms`，改为声明 `limits.max_gate_timeout_ms`（RINO 计划 600000），所有 gate 超时 MUST 不超过它。
- `turn.stop` 默认维持 120 秒。consumer MAY 经 `consumer.json.timeouts_ms` 申请更长（建议上限 300 秒，验收要跑测试）；producer 按 §3.3 夹紧（CC 为 170000）并报 `partially_applied`。
- 等待 MUST 可取消：收到 SIGINT / cancel 时 producer 立刻按 `default_decision` 写 ack（`via: error`）。

### 9.3 turn.stop 的默认值（讨论与提议）

现状（9f478f8）：Stop 超时、reply 无效、`stop_hook_active` 为真但没有明确 block、阻断次数到上限——全部**放行结束**。这和 deny 方向看起来相反，但「结束本轮」本身是没有副作用的状态：智能体停下，不再动手。反过来，出错时 block 会让智能体在没人监督的情况下继续干活，那才是 fail-open。

提议（v1 规范采用）：

1. `turn.stop` 的 `default_decision` 是 `ack`（结束本轮）。producer MUST 在 ack 文件里写 `accepted: false`，`via` 为 `timeout|error|invalid_reply`。
2. consumer MUST NOT 把 `accepted: false` 的结束当作验收通过；SHOULD 把任务状态标成「未验收」并报告。
3. 只有 consumer 明确 `decision: ack` 时 `accepted: true`。
4. producer SHOULD 把「未验收」带进自己的最终结果，例如 RINO 的结果行写 `accepted: false` 并以退出码 1 结束；CC 适配层可在 `session.end` 的 payload 里带 `accepted`。
5. 防死循环：producer MUST 有 `max_blocks`（默认 3）。第 `max_blocks + 1` 次 block MUST 被当作 ack，ack 文件写 `accepted: false`、`reason` 说明达到上限。`stop_active`（CC 的 `stop_hook_active`）为真时，只有显式 block 才会再次阻断。

### 9.4 迟到、无效与 ack 文件

producer 对每个 gate MUST 写且只写一个 `acks/<seq>.json`（`ack.schema.json`）：

| status | 情形 | applied_decision |
| --- | --- | --- |
| `consumed` | deadline 前读到合法 reply 并执行 | reply 的决定（`turn.stop` 时可能被 `max_blocks` 改成 ack，`via: rule`） |
| `invalid` | deadline 前读到的 reply 不合法 | `default_decision`；`errors` 列出原因 |
| `expired` | deadline 前没有合法 reply（包括没有 reply、或 reply 在 deadline 后才出现） | `default_decision` |

- ack 在决定**执行之前**写出（崩溃时 consumer 至少知道 producer 打算做什么）；带 `additional_context` 的 reply 也一样，先写 ack 再注入（§7.3）。
- 非 gate 事件不写 ack，唯一例外是 §7.3 第 1 条：consumer 对非 gate 事件写了 reply 时的 `invalid` ack。
- deadline 之后才出现的 reply：producer 若仍在运行，SHOULD 检查一次，但 ack 已经是 `expired`，MUST NOT 改写。consumer 写 reply 后 SHOULD 读 ack 确认结果，看到 `expired` 时 MUST 认为自己的回答没有生效。
- `reply_sha256` 记录被执行的 reply 文件字节的 SHA-256。
- producer 自身出错（§9.1）时 ack 写 `status: expired`、`via: error`。
- 静态规则直接拒绝、没有写 pending 的 `tool.pre` 不写 ack（它没有等待 consumer），只有 `events.jsonl` 记录和 `policy.deny`。

## 10. 时间

- 所有时间戳 MUST 是 RFC 3339 且带偏移。producer SHOULD 用本地偏移（如 `+08:00`），consumer 比较时 MUST 按绝对时间比较。
- producer 判断 deadline MUST 用单调时钟换算，不得受系统时间调整影响；对外写的 `deadline` 是开始等待时的墙钟时间加超时。
- 沙箱被暂停（如 E2B AutoPause）后恢复时，若墙钟已过 deadline，producer MUST 立即按 `expired` 处理（见 §14）。

## 11. 静态规则（policy deny）

- producer MUST 在发出 `tool.pre` gate 之前评估静态规则。按顺序，第一条命中的规则生效。
- 命中即 deny：MUST NOT 写 pending，MUST NOT 被 reply、调试开关（CC 的 `wait_all`、`CC_BRIDGE_PROBE`）或 `consumer.json` 覆盖。
- producer MUST 在 `events.jsonl` 记 `outcome.via = rule` 和 `rule_id`，并在 `features.policy_deny_event` 为真时发 `policy.deny` notify（9f478f8 尚未实现，Phase 2 补上），让 consumer 看见被拦了什么。
- 规则内容不在协议里标准化，但 `capabilities.json` MUST 公布规则 id、规则文件 sha256 和每条规则的 `status`。
- 规则的工具筛选：`tools`（宿主工具名，宿主专用）和 `tool_kinds`（§4.3 的规范化类别，可移植）任一命中即适用；只写了 `tool_kinds` 的规则对没有 `tool_kind` 的事件不适用。`command_regex` 匹配规范化的 `command`（shell 类）或宿主命令字符串。
- 规则 MAY 带 `required: true`。producer 不能完整执行某条规则（不认识的匹配键、工具名对不上、拿不到 `command`）时，MUST 把它标成 `partially_applied` / `unapplied`；consumer 见到 required 规则不是 `applied` 时 SHOULD 拒绝开始（§3.3）。v1 推荐的内置规则：递归强删（`rm -rf`、`Remove-Item -Recurse -Force`）、强推（`git push --force/-f/--force-with-lease`）、读写 `.env`（放行 `.env.example/.sample/.template`）、访问 `~/.ssh`。另有通用 `command_regex`、`path_regex`。
- 规则本身出错（正则无效、规则文件损坏）：MUST NOT 静默跳过。规则文件损坏 MUST 回退内置规则；单条正则无效时，该规则 SHOULD 对其适用工具一律 deny，并发 `producer.error`。
- `consumer.json.extra_rules` 只能追加 deny 规则，排在内置规则之后。

## 12. 脱敏与路径信任

12.1 脱敏

- producer MUST 在写任何文件（包括 `.full.json`、`events.jsonl`、ack）之前脱敏，替换为 `[redacted]`。
- MUST 至少覆盖：环境里已知的密钥变量值（CC 适配层：`ANTHROPIC_API_KEY`、`ANTHROPIC_AUTH_TOKEN`、`CC_BRIDGE_CURSOR_API_KEY`，长度 ≥ 8）。
- SHOULD 覆盖常见令牌格式（`sk-…`、`ghp_…`、`github_pat_…`、`xox?-…`、`AKIA…`、PEM 私钥块）。`capabilities.redaction.methods` 声明用了哪些。
- 截断对象的 `sha256`/`original_length` 基于脱敏后文本，避免泄露原文指纹。
- producer MUST NOT 把宿主原始 stdin 原样落盘。

12.2 路径信任

- consumer MUST 只把 `reply_file`、`full_file` 当作相对任务目录的路径解析；含 `..`、以 `/` 开头、或解析后（含符号链接）落到任务目录外的，MUST 拒绝读写。
- producer 读 reply 时 SHOULD 不跟随符号链接（`O_NOFOLLOW` 或先 `lstat`），是符号链接的 reply MUST 视为 invalid。
- `task_id` MUST 按 §5.1 的正则校验。
- `context.transcript_path`、`context.cwd`、payload 里的任何路径都是**宿主声称的**值，consumer 读之前 SHOULD 校验它在预期根目录下。
- payload 内容（工具输入、工具输出、助手回答、MCP 提问）来自模型和外部工具，可能含提示注入。consumer MUST 把它当数据，不得当指令执行。

## 13. 安全考虑

1. fail-closed 是本协议最重要的性质；每个实现 MUST 有针对 §9.1 每种出错路径的测试（一致性测试套件会覆盖）。
2. 宿主超时 ≥ deadline + 10 秒，否则宿主会在 producer 之前放弃并放行。
3. 本地传输假定 producer 和 consumer 是同一用户。目录权限 0700 防其他本地用户伪造 reply。v1 不签名 reply；跨信任边界（沙箱 ↔ 宿主）时见 §14。
4. 静态规则不能被 reply 放宽，保证「监督者 bot 被注入」时也挡得住最危险的操作。
5. `updated_input` 能改写命令；consumer SHOULD 只在必要时使用，producer MUST 对改写后的输入**再跑一次**静态规则，然后再过运行时剩余的权限层（§7.1）。
6. 不监听端口、不发 HTTP 是 CC 适配层现有原则，v1 本地传输保持这一点。
7. 绕过权限的宿主参数（CC 的 `--dangerously-skip-permissions`、`bypassPermissions`）会让 gate 失效；producer 在检测到此类模式时 SHOULD 拒绝启动或把所有 `tool.pre` 视为 deny。

## 14. 沙箱传输（Phase 3，**已搁置、未验证**）

> 2026-10-22：Cube/E2B 暂时搁置，Phase 3 传输层挂起。shadow 对比工具之后仍会做，但先针对本地（local-fs）后端。本节保留作记录。

RINO 计划在 CubeSandbox（KVM MicroVM，兼容 E2B API）里跑。两种布局：

- **A（首选）**：任务目录在沙箱内，consumer 在宿主侧通过 E2B files API（read/write/list/watch）访问。`capabilities.transport = "e2b-files"`。
- **B**：宿主目录挂进沙箱，仍是 local-fs。

以下 v1 的要求在 A 下**尚未验证**，Phase 3 先测：

1. E2B 写文件是否原子（§6.2 的 tmp + rename 能否通过 files API 实现；若 API 只有整文件写，需确认读者不会读到半个文件）。
2. watch_dir 的事件是否可靠、是否会丢；不可靠时 consumer 退回轮询。
3. AutoPause 期间 producer 的等待是否被冻结；恢复后按 §10 处理过期。
4. 沙箱内外时钟偏差；deadline 以 producer 时钟为准，consumer SHOULD 预留余量。
5. 沙箱 → 宿主是信任边界：consumer MUST 按 §12 校验一切路径和内容；是否需要给 reply 加 HMAC 留到 v1.1 讨论。

---

## 附录 A：Claude Code 适配层（cc-cursor-bridge）

本附录描述 CC 适配层怎样实现 AHP v1，以及 9f478f8 的现状与差距。§A.6 的差距在 Phase 2 修复，本分支不改代码。

### A.1 hook ↔ 事件

机器可读版本：`spec/tools/ahp_map.py`（`CC_TO_AHP`、`DECISIONS`、`DEFAULT_DECISION`、`SYNTHESIZED`）。

合成事件：PostToolUse 且 `tool_name = TodoWrite` 时，适配层除 `tool.post` 外 MUST 再发一条 `plan.update`（`items` 取 `tool_input.todos` 的 `content`/`status`，`source: "TodoWrite"`）。

`tool_kind` 映射：Bash、PowerShell → `shell`（`command` 即原命令）；Edit、MultiEdit、Write、NotebookEdit → `edit`；Read → `read`；Grep、Glob → `search`；WebFetch、WebSearch → `fetch`；AskUserQuestion → `ask`；其余（含 MCP 工具、Skill、Task）→ `other`。

limits：`host_hook_timeout_ms = 180000`，因此 gate 超时上限 170000；也写 `max_gate_timeout_ms = 170000`。

hook 同步/异步配置在 `claude/hooks/hooks.json`：四个 gate（PreToolUse、PermissionRequest、Elicitation、Stop）和 SessionStart、WorktreeCreate 同步（timeout 180 秒），其余 `async: true`（30 秒，SessionEnd 55 秒）。异步 hook 不能阻塞 CC，所以只有同步 hook 能被 consumer 升为 gate；把异步 hook 升为 gate 需要同时改 `hooks.json`。

### A.2 回复 → hook 输出

| AHP 事件 / decision | CC stdout（exit 0） |
| --- | --- |
| `tool.pre` allow/deny/ask | `{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"<d>","permissionDecisionReason":"<reason>","updatedInput"?,"additionalContext"?}}` |
| `permission.request` allow/deny | `{"hookSpecificOutput":{"hookEventName":"PermissionRequest","decision":{"behavior":"<d>","message"?(deny),"updatedInput"?}}}` |
| `input.request` accept/decline | `{"hookSpecificOutput":{"hookEventName":"Elicitation","action":"accept|decline","content"?}}` |
| `turn.stop` block | `{"decision":"block","reason":"<reason>"}` |
| `turn.stop` ack | 空 stdout |
| `model.switch.pre` | `hookSpecificOutput.permissionDecision` |
| `session.start` | `hookSpecificOutput.additionalContext` / `watchPaths` |
| `worktree.create` | stdout 只有 worktree 绝对路径一行 |
| 其他 block 类 | 顶层 `{"decision":"block","reason":…}`，`additional_context` → `hookSpecificOutput.additionalContext` |

`additionalContext` 的宿主上限：CC 文档写明每个 hook 的每个 `additionalContext` 字符串上限 10,000 字符（不可配置）；超出时 CC 把全文存到会话目录的文件里，只注入文件路径和前 2,000 字符的预览，且不提示智能体去读——等于静默截断。CC 适配层因此声明 `limits.max_additional_context_chars = 10000`（配置只能调小），按 UTF-16 码元计数（与 CC 的 JavaScript 字符串长度一致，码点数不会更大），超出即 invalid。

`prompt.submit` 在 CC 上：UserPromptSubmit 在 `hooks.json` 里是异步的，所以 CC 适配层不提供它作为 gate（`allowed_classes` 里没有 `gate`），§9.1 表里的 `prompt.submit` 一行在 CC 上不适用；要支持，需把该 hook 改为同步。

### A.3 退出码（按 CC 文档，**未在 2.1.306 上逐一复测**）

- `0`：读 stdout JSON。
- `2`：阻塞错误，stderr 回给模型。对 PreToolUse 等于拒绝工具；对 UserPromptSubmit 拒绝提示；**对 Stop 等于阻止结束（让 CC 继续）**。
- 其他非零：非阻塞错误，CC 继续——对 gate 是 fail-open。

因此 CC 适配层的 fail-closed 规则：PreToolUse / PermissionRequest / Elicitation 出错时 MUST 输出显式 deny/decline JSON 并 exit 0；连 JSON 都写不出时 exit 2。**Stop 出错时 MUST exit 0 且 stdout 为空**（结束本轮），绝不能 exit 2。

### A.4 环境变量（9f478f8）

| 变量 | AHP 对应 |
| --- | --- |
| `CC_TASK_ID` | `task_id`（缺省用 CC 的 `session_id`） |
| `CC_BRIDGE_DIR` | `<root>`，默认 `~/.cc-bridge` |
| `CC_BRIDGE_CONFIG`、`CC_BRIDGE_RULES` | producer 配置、规则文件 |
| `CC_BRIDGE_EVENT_MODE_<Hook>` | 事件类覆盖（`wait`=gate、`push`=notify、`log`、`off`）；AHP 下改由 `consumer.json.event_classes` 表达，变量保留为调试用 |
| `CC_BRIDGE_WAIT_ALL`、`CC_BRIDGE_WAIT_ALL_TIMEOUT` | 调试：全部 gate；超时对应 `timeout_ms` |
| `CC_BRIDGE_ASSISTANT_TEXT` | `message.delta` 升为 notify |
| `CC_BRIDGE_WATCH` | SessionStart 注入 `watchPaths` |
| `CC_BRIDGE_STOP_MAX_BLOCKS` | `limits.max_blocks` |
| `CC_BRIDGE_PROBE` | 测试用的固定回答；MUST NOT 覆盖规则拒绝（现状已满足） |

### A.5 旧字段别名（只在 CC 适配层接受，AHP consumer 不应再写）

| 旧 | AHP |
| --- | --- |
| `{"ack": true}` | `{"decision": "ack"}` |
| `behavior` | `decision` |
| `message` | `reason` |
| `updatedInput` / `additionalContext` / `watchPaths` / `worktreePath` / `displayContent` | snake_case 同名 |
| Elicitation `allow` / `deny` | `accept` / `decline` |
| PreToolUse `defer` | 无对应；适配层可接受 `x-defer` 扩展 |
| 旧通知是扁平对象（`seq`、`event`=CC hook 名、`created_at`、`summary`、`input_keys`、`reply_hint`，字段在顶层） | envelope + `payload` + `context` + `native`；`legacy_to_ahp.py` 给出逐字段转换 |
| 文件名 `<seq>-<HookName>.json` | `<seq>-<ahp.event>.json` |
| `push/cursor`（由 `bridge.py push` 写） | `consumer/push-cursor` |
| `stop-blocks`（任务目录根） | `state/stop-blocks` |
| 旧 `wait`（`CC_BRIDGE_WAIT`）的 `pending/<tool_use_id>.json` | 废弃 |

过渡期适配层 SHOULD 同时写旧路径和新路径（双写），现有 bot 的 `pending`/`push`/`watch` 输出（路径在第一列，TAB 后接 `# reply:` 等预览）保持不变。

### A.6 9f478f8 与 v1 的差距（Phase 2 修）

| # | 现状 | v1 要求 |
| --- | --- | --- |
| 1 | `dispatch.py` 捕获任何异常后 `return 0`，不输出 → CC 走自己的权限流程（fail-open） | 按 A.3 输出显式 deny/decline |
| 2 | stdin 不是 JSON 时 exit 0 无输出 | 同上 |
| 3 | PreToolUse 的 reply 是对象但 `decision` 不合法（拼错、缺失）→ `apply_reply` 返回空 stdout（等于没表态） | invalid → deny，写 ack |
| 4 | reply 文件是坏 JSON 时一直轮询直到超时 | MAY 继续等合法 reply；到期 → `expired`，ack 里注明见过无效 reply |
| 5 | 无 `capabilities.json`、`acks/`、`consumer.json` | 补上 |
| 6 | 规则命中只写 events.jsonl，consumer 看不到 | 发 `policy.deny` |
| 7 | 单条规则正则无效时跳过该规则 | 该规则对适用工具 deny，发 `producer.error` |
| 8 | `updated_input` 不再过规则 | 改写后重新评估规则 |
| 9 | 等待用 `time.monotonic()`，但 pending 不带 `deadline` | 写 `deadline`、`timeout_ms`、`decisions`、`default_decision` |
| 10 | 脱敏只覆盖 3 个环境变量的精确值 | 加令牌格式（SHOULD） |
| 11 | 临时文件 `<name>.tmp`（不以 `.` 开头，固定名，多写者会撞） | `.<name>.<pid>.<rand>.tmp` |
| 12 | 截断对象里没有 `full_file`（只在顶层） | 两处都写 |
| 13 | Stop 的 `accepted` 状态没有落盘 | ack 写 `accepted` |
| 14 | UserPromptSubmit 的 push 通知不带 `prompt` 正文（`summarize` 不收这个键），`legacy_to_ahp.py` 转出的 `prompt.submit` 因此不过 schema | 带 `prompt`（按 `content_chars` 截断） |
| 15 | Stop/SubagentStop 通知里的 `transcript_path` 等路径也按 `content_chars` 截断成截断对象；路径一旦被截就没法用 | 路径字段 MUST 是完整字符串，不截断（schema 已要求 string） |

## 附录 B：RINO P0 对表结论（draft.2）

RINO 开发的对表（`p0-mapping.md`，基于 RINO 根仓 8118bd5）回答了 draft.1 的十个问题，达成以下共识，已写进正文：

| # | 共识 | 正文位置 |
| --- | --- | --- |
| 1 | `plan.update` 进标准（notify）；厂商事件用 `x-<vendor>.`（如 `x-rino.goal.change`） | §2.3、§4.2、§4.3、A.1 |
| 2 | `tool.pre` 加可选 `tool_kind` 与 shell 类 `command`（POSIX quote 拼接）；规则加 `tool_kinds`；不能执行的规则标 `partially_applied/unapplied`；`required` 规则未完全执行时 consumer SHOULD 拒绝开始 | §3.3、§4.3、§11 |
| 3 | `host_hook_timeout_ms` 可缺席，改用 `limits.max_gate_timeout_ms`；`turn.stop` 默认 120 秒，consumer 可申请到 300 秒，producer 夹紧并报 `partially_applied` | §3.3、§9.2 |
| 4 | gate 可用「中断 → 外部裁决 → 恢复」实现；审批中断以 `permission.request` 发出时可不发 `tool.error` | §4.2、§4.4 |
| 5 | `message.delta` 可选 `kind: text\|reasoning`（reasoning 默认关）；`message_id` 不透明；`index` 每条消息从 0 连续；最后一片 `final` | §4.3 |
| 6 | 未验收 SHOULD 进入 producer 最终结果（RINO：`accepted:false` + 退出码 1） | §9.3 |
| 7 | `updated_input` 后先重跑静态规则，再走剩余 gate；改工具名的 reply 无效 | §7.1、§13 |
| 8 | 子代理共用 `task_id`，以 `context.agent_id` 区分 | §4.3 |

RINO 侧的分阶段声明（供 consumer 参考，不是规范要求）：第一版只声明 `tool.pre`、`turn.stop` 两个 gate，`permission.request`、`input.request` 等中断→恢复接好后再声明；`updated_input`、`additional_context` 先为 false。

仍开放：

1. `reasoning` 片段打开的方式：先用 `consumer.json.features.reasoning_delta`，是否需要单独的事件名，留到 v1.1。
2. 沙箱传输（§14）随 Cube/E2B 搁置。

## 变更记录

- **1.0.0-draft.3 补充**（2026-10-22，RINO）：§2.2 `spec_version` 接受 `-draft.<N>` 预发布后缀，producer SHOULD 写确切草案，consumer 比较时忽略后缀、MAY 警告；schema：`common.$defs.spec_version`，`capabilities`、`consumer`（新增可选）引用它；示例改为 `1.0.0-draft.3`；一致性检查 G18。
- **1.0.0-draft.3**（2026-10-22）：§7.3 注入上下文——只认 gate reply，对非 gate 的 reply 写 `invalid` ack；`limits.max_additional_context_chars`（缺省 10000，超出为 invalid、不截断）；ack 先于注入；持久化为会话消息（说明性）。§7.2 reply 带不属于该事件的字段为 invalid。§9.1 逐事件 fail-closed 表（`session.start` 不注入、`prompt.submit` block）。附录 A.2 记下 CC 的 10,000 字符上限。schema：`capabilities.limits` 加 `max_additional_context_chars`。一致性测试新增 4 个场景（`context-notify-reply`、`context-limit`、`session-start-failclosed`、`prompt-submit-failclosed`）。

- **1.0.0-draft.2**（2026-10-22）：附录 B 的 8 项共识；ack 在 producer 出错与规则拒绝时的写法（§9.4）；§14 标注搁置；schema：新增 `events/plan.update`，`tool.pre` 加 `tool_kind`/`command`，`message.delta` 加 `kind`，`capabilities.rules` 加 `status`/`required`，`limits` 加 `max_gate_timeout_ms`，`consumer.extra_rules` 加 `tool_kinds`/`required`；新增一致性测试套件 `conformance/`。
- **1.0.0-draft.1**（2026-10-22）：初稿。
