# AHP v1 conformance suite

Checks that a producer (Claude Code adapter, RINO, …) follows `spec/ahp-v1.md`.
Stdlib only; schema checks use `jsonschema` when installed and are skipped otherwise.

```bash
python3 -m conformance                 # replay + legacy bridge + cc-adapter + static fixture
python3 -m conformance --producer cc-adapter   # the AHP v1 Claude Code adapter in this checkout
python3 -m conformance --producer replay -v
python3 -m conformance --producer "python3 /path/to/rino-adapter.py"
python3 -m conformance --list          # scenarios and generic checks
python3 -m conformance --json report.json --keep
python3 -m conformance.selftest        # mutation test: broken reference producers must be caught
```

Exit code 0 means every failure is an expected one: the legacy bridge (and the
static fixture) may fail only the checks in `expected/legacy-9f478f8.json`;
any other producer only those in `expected/<producer>.json` (none for replay;
`expected/cc-adapter.json` is empty on purpose). An XPASS (an expected failure
that now passes) is printed so the list can be shrunk with `--update-expected`.

## Pieces

| Path | What |
| --- | --- |
| `harness.py` | runs one scenario: temp root, optional `consumer.json`, scripted consumer, producer adapter, checks |
| `consumer.py` | scripted consumer: replies on cue (allow/deny/ask/block/ack/accept/decline/invalid/raw/late/none); `source: "push"` items reply to notify events (spec 7.3 #1) |
| `checks.py` | generic checks G01–G17 over a task dir, scenario expectations E00–E13 |
| `producers/replay.py` | reference producer: replays scenario host events and implements the producer side of the spec |
| `producers/legacy_bridge.py` | drives `scripts/dispatch.py` as of 9f478f8 (extracted with `git archive` into `$TMPDIR/ahp-conformance-<uid>/`) per step, converts with `legacy_to_ahp.convert_dir` |
| `producers/cc_adapter.py` | drives this checkout's hooks.json commands (`dispatch.py <Hook>`) per step; the bridge writes the AHP tree itself |
| `scenarios/*.json` | 26 scenarios (`tools/build_scenarios.py`, `tools/snapshot_to_scenario.py` regenerate them) |
| `fixtures/legacy-9f478f8-demo/` | `demo-bridge-snapshot` converted to AHP shape, checked statically |
| `expected/legacy-9f478f8.json` | expected failures of the pre-AHP bridge (9f478f8) |
| `expected/cc-adapter.json` | expected failures of the AHP adapter (empty) |
| `MAPPING.md` | the 36 bridge unit tests → scenarios / N/A |

## Adapter contract (how to plug in a producer)

A producer under test is a command:

```text
<cmd> --describe
    -> one JSON line: {"name", "version", "watch": "tasks/{task}", "effects": true}
<cmd> --scenario FILE --root ROOT --task TASK
```

- Read `FILE` (scenario JSON) and feed each `steps[i]` (`{event, payload, context?, fault?}`) to the
  runtime as a host event, in order. `fault: "crash"` = make the producer fail internally while handling
  the event; `fault: "bad_input"` = deliver unparseable host input.
- Write AHP artifacts to `ROOT/tasks/TASK/` (the consumer may already have put `consumer.json` there).
  `watch` tells the harness where pending/ and replies/ live while running, if not there.
- SHOULD append `ROOT/harness/effects.jsonl` lines `{"step": i, "event", "effect"}`: what the host
  really did (`allow|deny|ask|accept|decline|block|ack|host-default|none`), plus `"injected": bool` —
  whether the host actually received `additional_context` (draft.3 scenarios check it). Without it the harness
  falls back to `events.jsonl` outcomes, which cannot catch a host that ignores the producer.
- Exit 0 after the last step. Gate timeouts in scenarios are short (1.5–3 s) via `producer.timeout_ms`.
- Static rules in scenarios use the bridge `rules.json` format (+ `tool_kinds`, `required`), the
  reference rule language. Secrets to redact arrive as env vars (`producer.env`).

## Checks

| id | check |
| --- | --- |
| G01 | capabilities.json present and valid |
| G02 | pending/push envelopes (and .full.json) valid |
| G03 | events.jsonl: one valid record with outcome per event |
| G04 | acks valid |
| G05 | seq unique, positive, every notice logged, counter ≥ max |
| G06 | no stray temp files |
| G07 | exactly one ack per gate (pending or producer error) |
| G08 | ack agrees with outcome; non-consumed ⇒ default decision |
| G09 | gate defaults are fail-closed; deadline after ts |
| G10 | truncation objects: sha256 / original_length / prefix / full_file |
| G11 | file names, reply_file, class vs directory, paths stay inside the task dir |
| G12 | event classes match capabilities |
| G13 | static rule deny: no pending, no ack, policy.deny emitted |
| G14 | no planted secret in any producer file |
| G15 | expired acks applied the default (late replies ignored; turn.stop accepted:false) |
| G16 | consumer timeouts clamped to host−10 s or max_gate_timeout_ms, reported partially_applied |
| G17 | rules declared with status; required rules applied |
| E00–E13 | per-scenario expectations: effect, via, ack status, accepted, pending, class, truncated fields, payload fields, policy.deny count, producer.error, capabilities classes, context injected (E12), capabilities limits (E13). A scenario with `requires_gate` is skipped (E00 skip) when the producer does not offer that event as a gate |
| P00 | producer exited 0 |
