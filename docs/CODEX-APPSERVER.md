# Codex app-server adapter

Supervised, auditable backend that drives the installed Codex CLI (0.167.0)
official **stdio app-server** JSON-RPC and writes an AHP-compatible task
tree so existing `scripts/bridge.py pending|reply|status --ahp` and
`scripts/supervise.py --ahp` keep working.

This is a **sibling** of the Claude Code hook adapter. It does not replace
the CC backend, does not register Claude hooks, and does not change skill
files.

## Why not `codex exec`

`codex exec` is permitted only for self-contained workspace edits. In Codex
0.167.0, headless `exec` resolves `approval_policy=on-request` to
`approval_policy=never` unless `approvals_reviewer=auto_review`. That is
**no escalation**, not a supervised human approval path. Do not describe
`codex exec` as supervised. This adapter is the supervised path.

## Invocation (model and effort are required)

```bash
python3 scripts/run_codex_bridged.py \
  --task TASK_ID \
  --cwd /path/to/workspace \
  --model gpt-6.1-sol \
  --effort medium \
  --prompt-file prompt.txt \
  --timeout-seconds 120
```

`--model` and `--effort` are required on every invocation. The adapter
passes them on `thread/start` and/or `turn/start`. It does **not** write
`~/.codex/config.toml` and does not read a shared default for these
fields.

Optional: `--codex-bin`, `--bridge-dir` / `CC_BRIDGE_DIR`,
`--ahp-dir` / `CC_BRIDGE_AHP_DIR`, `--jsonl` for a redacted RPC transcript.
Without `--prompt-file`, the prompt is read from stdin through EOF.
`--turn-timeout-seconds` bounds the entire turn (default 3600s), separately
from the gate timeout. Both timeouts must be finite and positive.

## Policy (fixed for this candidate)

- `approvalPolicy`: `on-request`
- `approvalsReviewer`: `user`
- sandbox: `workspaceWrite`, `networkAccess: false`
  (`writableRoots: [cwd]`, implicit `/tmp` and `$TMPDIR` write access disabled)
- Consumer replies: `allow` or `deny` only (`accept`/`decline` aliases).
  `allow` maps to one-shot Codex `accept`. `deny` and timeout map to
  `decline`. The command is not executed on deny, timeout, cancel, or
  disconnect.
- `acceptForSession`, execpolicy-amendment *replies*, network policy
  amendments, and permission grants are rejected. An invalid reply is
  treated as deny (fail-closed), not as a retry hook for a broader grant.
  A non-null `networkApprovalContext` is a managed-network approval prompt
  and is rejected immediately. When `availableDecisions` is advertised,
  it must include a one-shot `accept`/`allow` to permit an allow reply;
  denial remains available regardless of that menu. Codex 0.167 often
  attaches `proposedExecpolicyAmendment` and an
  `acceptWithExecpolicyAmendment` menu entry to a sandbox-blocked
  command; that proposal is audited and never applied. It does not by
  itself fail a one-shot accept/deny when `accept` is also advertised.
  The 2026-11-11 04:52 canary declined such a request before this
  distinction was enforced; do not treat that run as proof of the
  current menu/amendment policy.
- Supported server requests: `item/commandExecution/requestApproval` and
  `item/fileChange/requestApproval` (plus legacy `execCommandApproval` /
  `applyPatchApproval`, mapped with the generated ReviewDecision `denied`
  object). Extra fields on known messages are tolerated.
- Unknown or unsupported server requests are fail-closed. In particular,
  `item/permissions/requestApproval` has **no** decline field in the
  generated 0.167 `PermissionsRequestApprovalResponse`. The adapter does
  not invent a grant. It completes the JSON-RPC request with a
  `JSONRPCError` (generated schema) and calls `turn/interrupt`. This
  permissions path was not the 2026-11-11 canary proof (that proof used
  commandExecution + `decline`).
- Auth: uses the existing box login. This adapter never copies or reads
  `auth.json`. Shared `CODEX_HOME` (`~/.codex`) is an isolation
  limitation; this candidate does not invent a private auth setup.

## Task / request identity

Each approval is one AHP gate:

- `task_id` — `CC_TASK_ID` / `--task` (same `safe_component` rules as the
  CC bridge)
- `seq` — AHP sequence in `<ahp-root>/tasks/<task>/`
- Codex `request id`, `threadId`, `turnId`, `itemId` (and `approvalId`
  when present) stored on the pending envelope under `native` and in
  `state/codex-requests.jsonl`

A reply is applied only when `replies/<seq>.json` matches that pending
seq. Existing `python3 scripts/bridge.py reply TASK SEQ '{"decision":"deny"}' --ahp`
is the controller write path.

Approval requests cannot establish the active turn identity. An early
approval waits for `turn/start` or `turn/started`; an allow requires a
matching active thread and turn. Cached fileChange context is bound to
thread, turn, and item id; uncorrelated context is denied.

## Timeout, cancel, disconnect

- Gate timeout (default 120s, `--timeout-seconds`): default decision
  `deny` / Codex `decline`. No execution.
- Controller (this adapter process) death, stdin close, or disconnect:
  fail-closed. Pending approvals are declined or the turn is interrupted;
  the child app-server process group is terminated; `status.json` is
  written with `state: "ended"` (the only terminal value `supervise.py`
  recognizes), `success: false`, and a truthful `session_end_reason`
  (`cancelled` / `failed`) plus `turn_status` when known. Never report
  `turn_status: completed` or `success: true` if the turn did not complete.
- SIGINT and SIGTERM are handled as controller cancellation. Stdio close
  here means the app-server transport; prompt EOF is normal input completion.
  SIGKILL and host failure cannot run cleanup or write a final status. An
  outstanding gate remains non-successful on disk and is acknowledged as
  denied on the next invocation; the fixture exits on transport EOF.
- This candidate does **not** claim full reconnect or resume of an
  in-flight approval after controller death. The smallest safe behavior
  is cancel/fail-closed and a **new** turn (new `turn/start` on a later
  invocation). A stored `threadId` may be recorded for later work; do
  not treat it as a proven resume protocol.

## Observer detach vs controller death

`python3 scripts/supervise.py --ahp TASK` is a **read-only** observer. It
already detaches on SIGINT/SIGTERM without modifying the task tree and
without answering gates. Observer detach is not controller death and
must not cancel the Codex turn.

The adapter process is the controller. Only its death or an explicit
cancel/interrupt ends the turn fail-closed.

## Status and logs

- `<ahp-root>/tasks/<task>/status.json` — durable state. Terminal
  sessions always use `state: "ended"` so the existing supervisor can
  detach; include `success`, `session_end_reason`, and `turn_status`
  from `turn/completed` (`completed` | `interrupted` | `failed`) when
  known. Do not report success if the turn was cancelled or failed.
  Native `failed` or `interrupted` completions preserve that value as
  `session_end_reason`, keep `success: false`, and copy the native turn
  error into `status.error` when present. The terminal state stays `ended`.
  `success` is false and the CLI exits nonzero after a gate timeout, invalid
  reply, or unsupported request, even if a declined gate's turn subsequently
  completes. `last_gate` preserves the latest resolved gate outcome.
  A pre-existing `replies/<seq>.json` is stashed before a new gate of
  that seq is published and again immediately after publication. The
  controller records the published pending file's nanosecond mtime on
  the gate and in the audit log. Replies with an older file mtime are
  ignored. Stashed file identities also prevent restoring an allow by
  rename when filesystem timestamps tie. A fresh post-publication
  `write_atomic` reply remains valid. This does not resume an old gate
  or provide full reconnect.
- `events.jsonl` — redacted audit log.
- Optional `--jsonl` RPC transcript: secrets redacted; **token-usage
  metrics kept as numbers** (`inputTokens`, `totalTokens`, …). A key
  must not be treated as a secret merely because it contains “token”.

## Tests

```bash
python3 scripts/test_codex_appserver_core.py
python3 scripts/test_codex_appserver.py
```

Deterministic tests speak to a fake stdio protocol server, not to the
real Codex binary. They cover allow, deny, fileChange, timeout,
disconnect/controller death, unknown requests, permissions rejection,
invalid grant replies, identity correlation, and usage-preserving
redaction.

A bounded real integration (outside CI by default) may be run with
`CODEX_APPSERVER_INTEGRATION=1` when a box login already exists. It
must request an outside-workspace canary write, **decline** it, and
prove the canary was not created. It must never accept a dangerous
external write.

## Limitations

- Shared `CODEX_HOME` is not process-isolated.
- No full reconnect after controller death.
- `reasoningEffort` may be null on `thread/start` (that RPC has no
  effort field); effort is sent on `turn/start`. Absence of a server
  echo does not change what was requested.
- `item/permissions/requestApproval` is rejected, not interactively
  granted or declined through the command/fileChange result shape.
