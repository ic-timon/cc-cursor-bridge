# Hook harness report

Fired 28/33.

## wait_all

`CC_BRIDGE_WAIT_ALL=1` makes every synchronous hook write `pending/<seq>-<event>.json` and block on `replies/<seq>.json`. A plain Write outside the repo produced 8 pendings and finished in 12s with the stand-in replier (no PermissionRequest, because that path is not a sensitive file). The same Write inside the repo produced 9 pendings and finished in 15s when the agent allowed PreToolUse and PermissionRequest; `note.txt` contained `hi`. Events that were only acks in that run, and are the first candidates to stop waiting on later: SessionStart, UserPromptSubmit, PostToolUse, PostToolBatch, MessageDisplay, Stop, SessionEnd. PreToolUse and PermissionRequest are the ones that changed the Write outcome.

Plain Write of note.txt outside the repo succeeds. Inside the repo the same Write is a safetyCheck sensitive file.

| event | fired | notes |
| --- | --- | --- |
| SessionStart | yes | - |
| Setup | yes | - |
| InstructionsLoaded | yes | - |
| UserPromptSubmit | yes | - |
| UserPromptExpansion | yes | - |
| MessageDisplay | yes | - |
| PreToolUse | yes | - |
| PermissionRequest | yes | - |
| PermissionDenied | no | NOT_TRIGGERABLE in these runs. --permission-mode auto --model sonnet --allowedTools Bash curl -I https://example.com executed the curl (PostToolUse) and did not deny it. A second run without allowedTools, prompt `sudo true`, was still thinking at the 90s timeout (exit 124) with only SessionStart and UserPromptSubmit; no PermissionDenied. Earlier haiku debug: model does not support auto mode. |
| PostToolUse | yes | - |
| PostToolUseFailure | yes | - |
| PostToolBatch | yes | - |
| Notification | yes | - |
| SubagentStart | yes | - |
| SubagentStop | yes | - |
| TaskCreated | yes | - |
| TaskCompleted | yes | - |
| Stop | yes | - |
| StopFailure | no | NOT_TRIGGERABLE without breaking the proxy. ANTHROPIC_BASE_URL=http://127.0.0.1:9 plus ANTHROPIC_API_KEY=invalid-probe-key on that one process still returned subtype success, api_error_status null, result ok. cc-switch config was not edited. --model not-a-real-model-probe also completed. |
| TeammateIdle | no | NOT_TRIGGERABLE. `claude --agent-teams` exited with: error: unknown option '--agent-teams'. CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1 did not expose a teammate tool; the model ran ls and the 90s session ended with PermissionRequests and no TeammateIdle. |
| ConfigChange | yes | - |
| CwdChanged | no | NOT_TRIGGERABLE in the attempts made. Bash `cd sub && pwd` ran and the model reported the new directory, but no CwdChanged. EnterWorktree created .claude/worktrees/probe-cwd and fired WorktreeCreate, not CwdChanged. Docs: the event fires when the session working directory changes, for example when Claude executes a cd; neither of those headless paths changed the session cwd. |
| DirectoryAdded | no | NOT_TRIGGERABLE. --add-dir at launch did not emit it. stream-json `/add-dir <sandbox>/extra` returned exactly: /add-dir isn't available in this environment. |
| FileChanged | yes | Fired in /tmp gap run. SessionStart is sync and CC_BRIDGE_WATCH points at watched.txt; the file was rewritten between two stream-json turns. events=SessionStart,UserPromptSubmit,MessageDisplay,Stop,UserPromptSubmit,FileChanged,MessageDisplay,Stop,SessionEnd |
| WorktreeCreate | yes | Fired in /tmp gap run. claude -p --worktree (unnamed) 'Reply ok. Do not use tools or edit files.' events=WorktreeCreate,SessionStart,UserPromptSubmit,MessageDisplay,Stop,SessionEnd |
| WorktreeRemove | yes | Fired when the model called ExitWorktree with discard_changes true inside claude -p --worktree probe-exit. A plain --worktree exit did not remove it. |
| PreCompact | yes | Fired in /tmp gap run. stream-json: six 'reply ok' turns then /compact. result= |
| PostCompact | yes | Fired in /tmp gap run. stream-json: six 'reply ok' turns then /compact. result= |
| PreModelSwitch | yes | Fired in /tmp gap run. command: claude -p /model sonnet (hooks now synchronous). events=SessionStart,PreModelSwitch,SessionEnd |
| PostModelSwitch | yes | - |
| Elicitation | yes | - |
| ElicitationResult | yes | - |
| SessionEnd | yes | - |

## Write in claude -p

Inside repo: (a)(b)(d)(e) safetyCheck sensitive file; (c) PermissionRequest allow wrote the file; (f) permission-prompt-tool wrote the file.
Outside repo (/tmp/ccb-probe-xcoc): (a) with no hook decision and no extra permission flags wrote note.txt.

| id | setup | PermissionRequest | file written | exit | errors |
| --- | --- | --- | --- | --- | --- |
| write-a | no hook decision, default permission mode | yes | no | 0 | {"type": "system", "subtype": "permission_denied", "tool_name": "Write", "decision_reason": "Claude requested permissions to edit /Users/timon/Sync/Test/cc-cursor-bridge/examples/sandbox/hook-harness/run-20261021-193741/write-a.txt which is a sensitive file.", "decision_reason_type": "safetyCheck",  |
| write-b | PreToolUse permissionDecision allow | yes | no | 0 | {"type": "system", "subtype": "permission_denied", "tool_name": "Write", "decision_reason": "Claude requested permissions to edit /Users/timon/Sync/Test/cc-cursor-bridge/examples/sandbox/hook-harness/run-20261021-193741/write-b.txt which is a sensitive file.", "decision_reason_type": "safetyCheck",  |
| write-c | PermissionRequest decision allow | yes | yes | 0 | - |
| write-d | --permission-mode acceptEdits | yes | no | 0 | {"type": "system", "subtype": "permission_denied", "tool_name": "Write", "decision_reason": "Claude requested permissions to edit /Users/timon/Sync/Test/cc-cursor-bridge/examples/sandbox/hook-harness/run-20261021-193741/write-d.txt which is a sensitive file.", "decision_reason_type": "safetyCheck",  |
| write-e | --allowedTools Write | yes | no | 0 | {"type": "system", "subtype": "permission_denied", "tool_name": "Write", "decision_reason": "Claude requested permissions to edit /Users/timon/Sync/Test/cc-cursor-bridge/examples/sandbox/hook-harness/run-20261021-193741/write-e.txt which is a sensitive file.", "decision_reason_type": "safetyCheck",  |
| write-f | --permission-prompt-tool mcp__permit__approve | yes | yes | 0 | - |
