# spec/

AHP v1（Agent Hook Protocol，`ahp/1`）草案。

| 路径 | 内容 |
| --- | --- |
| `ahp-v1.md` | 规范正文（附录 A：Claude Code 适配层；附录 B：给 RINO 的开放问题） |
| `schemas/` | JSON Schema 2020-12：`envelope`、`reply`、`ack`、`capabilities`、`consumer`、`common`，`events/` 下是各事件 payload |
| `examples/` | 合法示例（多数来自 `demo-bridge-snapshot`，即 9f478f8 在 haiku 上跑的 calc demo）；`examples/invalid/` 下的必须被拒绝 |
| `tools/ahp_map.py` | CC hook → AHP 事件、默认类、决定集合、默认决定 |
| `tools/legacy_to_ahp.py` | 只读地把 9f478f8 的任务目录转换成 AHP envelope/reply（打印到 stdout） |
| `tools/validate.py` | 检查 schema 是否良构，并校验 `examples/`（有 `jsonschema` 时） |

```bash
python3 spec/tools/validate.py
python3 spec/tools/legacy_to_ahp.py ~/Sync/Test/ccb-capture-run/demo-bridge-snapshot --producer-version 9f478f8 > /tmp/ahp.jsonl
python3 spec/tools/validate.py --jsonl /tmp/ahp.jsonl
```

对 `demo-bridge-snapshot` 跑上面第三条，20 个实例里 19 个通过；`push/2-UserPromptSubmit.json` 不通过是已知差距（规范附录 A.6 第 14 条：旧通知不带 `prompt`）。

本目录不改变 bridge 行为；`scripts/`、`claude/hooks/` 在 Phase 2 才按附录 A.6 修改。
