# AHP shadow diff（`tools/shadow_diff`）

同一个任务分别交给两个 AHP v1 producer 跑：**参照**（reference，CC 适配层）和**候选**（candidate，RINO 本地执行后端，将来逐步替代 Claude Code）。shadow diff 读两边的任务目录（`<root>/tasks/<task>/`，spec §6.1），去掉噪声后逐事件比较，报告真正影响监督语义的差异：事件序列与类、哪些 gate 被提出及其决定、`tool.pre` 的规范化视图（`tool_kind`、`command`）、静态规则与 `policy.deny`、`turn.stop` 结果、超时、fail-closed，以及 `capabilities.json` 的差异。

只用 Python 标准库；加载复用 `conformance.checks.TaskView`，默认决定、类表复用 `conformance.common` / `spec/tools/ahp_map.py`。

## 用法

```bash
# 比较两棵已有的任务树（任务目录，或 AHP 根目录 + --task）
python3 -m tools.shadow_diff diff REF_TASK_DIR CAND_TASK_DIR \
    --config tools/shadow_diff/allow/cc-vs-rino.json --json result.json
python3 -m tools.shadow_diff diff ~/.cc-bridge/ahp ~/.rino/ahp --task T1 --config ...

# 用一致性套件的场景驱动两个 producer（同一个脚本化 consumer），再比较两棵树和宿主效果
python3 -m tools.shadow_diff run --reference cc-adapter --candidate "python3 /path/to/rino-adapter.py" \
    --all --config tools/shadow_diff/allow/cc-vs-rino.json --json shadow.json
python3 -m tools.shadow_diff run --reference cc-adapter --candidate replay --scenario gate-replies \
    --config tools/shadow_diff/allow/cc-vs-replay.json -v

python3 -m tools.shadow_diff.tests     # 本工具的测试（合成任务树 + 一次 run 模式）
```

- `-v`：同时列出被放行的差异、未用到的放行条目和对齐表；`--quiet`：不打印摘要。
- `--conformance`：另外对两棵树跑一致性通用检查（G01–G18），失败记为 D00。
- `--fail-on-warn`：把警告（D16）也算作失败。
- `run` 模式的候选命令遵守 `conformance/README.md` 的适配器约定（`--describe`、`--scenario/--root/--task`、`effects.jsonl`）。需要某个 gate 而有一边不提供的场景（E00 skip）记为 SKIP，不比较。

退出码：`0` 没有意外差异；`1` 有意外差异（或任务树缺 `events.jsonl`）；`2` 用法、输入路径或配置错误。

结果 JSON（`--json`）：`result`（pass/fail）、`reference` / `candidate`（目录、producer、事件数）、`counts`（critical / unexpected / warnings / allowed / aligned）、`divergences[]`（`code`、`kind`、`severity`、`path`、`event`、`ref_index`/`cand_index`、`ref_seq`/`cand_seq`、`ref`/`cand` 值、`message`、`allowed_by`）、`unused_allow`、`alignment[]`。`run` 模式外面再包一层 `{result, runs: [...]}`，每项多 `scenario`、`producer_exit`。

## 规范化（不比较的噪声）

| 噪声 | 处理 |
| --- | --- |
| `ts`、`deadline`、`created_at`、ack 的 `ts`、`reply_sha256` | 丢弃 |
| `seq` 的起点、空洞 | 按过滤后的序列**位置**比较；`policy.deny` / `producer.error` 的 `target_seq` 换成目标事件的位置，再经对齐映射比较 |
| `task_id`、`producer`、`context`（session、cwd、transcript）、`native`、`tool_use_id`、`x-*` 字段 | 丢弃 |
| 路径 | 任务目录 → `<task>`、AHP 根 → `<root>`、每个 `context.cwd` → `<cwd>`、`$HOME` → `<home>`（含 macOS `/private/tmp` 变体） |
| 截断 | payload 取 pending/push 文件（截断时取 `.full.json`），不取按 240 字符截断的审计日志；仍是截断对象的值按 `sha256` 与对面全文比较 |
| shell 命令 | `bash -c 'X'` / `sh -lc X` 解包成 `X`；按 shlex 词法重新 quote（`ls '/var/log'` == `ls /var/log`），空白归一 |
| 工具名 | 默认不比较 `tool_name`（`Bash` vs `run_command`），比较 `tool_kind` + `command`；`compare_tool_name: true` 可打开 |
| spec_version 的 `-draft.N` | 忽略（spec §2.2） |
| 厂商事件 `x-*` | 默认不比较（`ignore_events`） |
| log 类事件 | 两边都是 log 时不比较（`ignore_classes`）；一边升成 notify/gate 时照常比较并报 D03 |

## 对齐

先剥掉两端完全相同的前后缀，中间做加权 LCS：键（事件名 + 规范化命令 / 规则 id）相同得 3 分，只有事件名相同得 2 分，不同事件不能配对。所以多出的一次工具调用报成 D02，后面的事件仍然对得上；同位置命令不同报 D08 而不是一缺一多。中间部分超过 400 万格时退回 difflib 两遍对齐。

## 差异规则

| code | 类 | 何时报 | 严重度 |
| --- | --- | --- | --- |
| D01 | missing | 参照有、候选没有的事件（例如少了一个 gate） | error |
| D02 | extra | 候选多出的事件（例如多一次工具调用） | error |
| D03 | class | 同一事件生效类不同（只报这一条，不再展开 gate 字段） | error |
| D04 | decision | gate 的 `outcome.decision` / ack `applied_decision` 不同 | error |
| D05 | via | 决定来源不同：reply / timeout / rule / error / invalid_reply，ack `status` | error |
| D06 | gate-shape | 是否写了 pending、`decisions`、`default_decision` 不同 | error |
| D07 | ack | 一边缺 ack，或 `turn.stop` 的 `accepted` 不同 | error |
| D08 | payload | 比较的 payload 字段不同（`tool.pre` / `permission.request` 的 `tool_kind`、`command`；`plan.update.items`；`tool.error.is_interrupt`；`producer.error.applied_decision`、目标） | error |
| D09 | policy | 静态规则结果不同：`outcome.rule_id`、`policy.deny` 的规则、目标事件 | error |
| D10 | timeout | gate 的 `timeout_ms` 不同 | error |
| D11 | fail-open | 单边检查，与对齐无关：超时 / 无效 reply / 出错后采用的决定不是 `default_decision`；`turn.stop` 未验收却 `accepted` 不为 false；规则命中却放行；pending 或 capabilities 把 deny/decline/block 方向的默认决定改弱 | **critical，不能放行** |
| D12 | capabilities | 事件是否声明、`class`、`allowed_classes`（`decisions`/`default_decision`/`timeout_ms` 只在两边都能做 gate 时比）；`features`、`limits`、`rules.ids/status/required/sha256`、`redaction.methods`、`consumer_json`、`transport`、`protocol` | error |
| D13 | host-effect | （run 模式）宿主实际效果不同：`effect`、`injected`；`ack` 与 `none` 视为相同（`effect_equivalents`） | error |
| D16 | consumer | 两边 consumer 的回复不同，或 `consumer.json` 不同（忽略 `created_at`）：比较本身不公平 | warn |
| D00 | conformance | （`--conformance`）任一棵树没过一致性通用检查 | error |

## 配置与放行清单

`--config` 可重复，按顺序合并在默认配置上；文件里的 `include` 先加载。可设的键：`ignore_events`、`ignore_classes`、`payload_fields`（事件 → payload 字段路径）、`compare_tool_name`、`command`（`unwrap_shell_c`、`canonical_quoting`、`collapse_whitespace`）、`path_placeholders`、`effect_equivalents`、`conformance`、`fail_on_warn`、`allow`。

放行条目（`allow[]`）：`id`、`code` 或 `codes`、`reason` 必填；可选 `path`（fnmatch，匹配差异的 `path`）、`event`（字符串或列表，fnmatch）、`side`、`ref`、`cand`（值必须相等，`"<absent>"` 表示该边没有）。所有给出的键都匹配才放行。D11 永远不放行。没用上的条目在 `unused_allow` 里列出，方便清理。

随工具提供：

- `allow/cc-known.json`：CC 适配层自身的已知差异——10 个异步 hook 不能做 gate（`cc-async-not-gate`，只放行参照为 `["log","notify"]` 的情形）、`reasoning_delta=false`、`host_hook_timeout_ms`。
- `allow/cc-vs-replay.json`：CC 对一致性参考 producer（测试用，也是示例）。
- `allow/cc-vs-rino.json`：CC 对 RINO P2 第一版的计划差异（只声明 `tool.pre`、`turn.stop` 两个 gate；不声明子代理、worktree 等；`max_gate_timeout_ms`；`updated_input`、`additional_context` 先关；`turn.stop` 可接受 300 秒；规则文件 digest 不同）。是草案，RINO 声明更多能力后逐条删。不比较 `message.delta`（两边分片方式不同）。
