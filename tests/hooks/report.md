# Hook harness report

Sandbox: `/Users/timon/Sync/Test/cc-cursor-bridge/examples/sandbox/hook-harness/run-20261021-193741`
Model: `haiku`. 

| event | fired | payload keys | decision tested | decision honoured | notes |
| --- | --- | --- | --- | --- | --- |
| SessionStart | yes | cwd, hook_event_name, session_id, source, transcript_path | none | n/a | - |
| Setup | yes | cwd, hook_event_name, session_id, transcript_path, trigger | none | n/a | - |
| InstructionsLoaded | yes | cwd, file_path, hook_event_name, load_reason, memory_type, session_id, transcript_path | none | n/a | - |
| UserPromptSubmit | yes | cwd, hook_event_name, permission_mode, prompt, prompt_id, session_id, transcript_path | none | n/a | - |
| UserPromptExpansion | yes | command_args, command_name, command_source, cwd, expansion_type, hook_event_name, permission_mode, prompt, prompt_id, session_id, transcript_path | none | n/a | - |
| MessageDisplay | yes | cwd, delta, final, hook_event_name, index, message_id, prompt_id, session_id, transcript_path, turn_id | none | n/a | - |
| PreToolUse | yes | cwd, hook_event_name, mcp_server, permission_mode, prompt_id, session_id, tool_input, tool_name, tool_use_id, transcript_path | probe-deny:applied, write-b:applied | yes | - |
| PermissionRequest | yes | cwd, hook_event_name, permission_mode, permission_suggestions, prompt_id, session_id, tool_input, tool_name, transcript_path | write-c:applied | see write experiments | - |
| PermissionDenied | no | - | none | n/a | NOT_TRIGGERABLE here: Write denials were safetyCheck/config rejects and did not emit PermissionDenied. Auto mode, which is what this hook documents, could not be entered on haiku (debug: model does not support auto mode). |
| PostToolUse | yes | cwd, duration_ms, hook_event_name, mcp_server, permission_mode, prompt_id, session_id, tool_input, tool_name, tool_response, tool_use_id, transcript_path | none | n/a | - |
| PostToolUseFailure | yes | cwd, duration_ms, error, hook_event_name, is_interrupt, permission_mode, prompt_id, session_id, tool_input, tool_name, tool_use_id, transcript_path | none | n/a | - |
| PostToolBatch | yes | cwd, hook_event_name, permission_mode, prompt_id, session_id, tool_calls, transcript_path | none | n/a | - |
| Notification | yes | cwd, hook_event_name, message, notification_type, prompt_id, session_id, transcript_path | none | n/a | - |
| SubagentStart | yes | agent_id, agent_type, cwd, hook_event_name, prompt_id, session_id, transcript_path | none | n/a | - |
| SubagentStop | yes | agent_id, agent_transcript_path, agent_type, background_tasks, cwd, hook_event_name, last_assistant_message, permission_mode, prompt_id, session_crons, session_id, stop_hook_active, transcript_path | none | n/a | - |
| TaskCreated | yes | cwd, hook_event_name, prompt_id, session_id, task_description, task_id, task_subject, transcript_path | none | n/a | - |
| TaskCompleted | yes | cwd, hook_event_name, prompt_id, session_id, task_description, task_id, task_subject, transcript_path | none | n/a | - |
| Stop | yes | background_tasks, cwd, effort, hook_event_name, last_assistant_message, permission_mode, prompt_id, session_crons, session_id, stop_hook_active, transcript_path | none | n/a | - |
| StopFailure | no | - | none | n/a | NOT_TRIGGERABLE without a real API failure. --model not-a-real-model-probe still completed and replied ok through the proxy. |
| TeammateIdle | no | - | none | n/a | NOT_TRIGGERABLE: claude -p has no agent-team teammate lifecycle. TeammateIdle fires when a teammate is about to go idle, and this CLI session never starts a team. |
| ConfigChange | yes | cwd, file_path, hook_event_name, prompt_id, session_id, source, transcript_path | none | n/a | - |
| CwdChanged | no | - | none | n/a | NOT_TRIGGERABLE in this headless run: scenario cwd asked Bash to `cd sub && pwd`. That did not emit CwdChanged. There is no separate -p cd control. |
| DirectoryAdded | no | - | none | n/a | NOT_TRIGGERABLE: --add-dir at launch did not emit DirectoryAdded, and claude -p /add-dir extra returned "/add-dir isn't available in this environment." |
| FileChanged | no | - | none | n/a | NOT_TRIGGERABLE with the current plugin: FileChanged has no filename matcher, and SessionStart is async so a watchPaths reply is ignored. Touching cc-bridge-probe-watch during baseline did not fire it. |
| WorktreeCreate | yes | cwd, hook_event_name, name, session_id, transcript_path | none | n/a | - |
| WorktreeRemove | no | - | none | n/a | --worktree fired WorktreeCreate, but exiting that -p process did not fire WorktreeRemove. |
| PreCompact | no | - | none | n/a | NOT_TRIGGERABLE: claude -p /compact returned 'Not enough messages to compact' and did not emit PreCompact or PostCompact. |
| PostCompact | no | - | none | n/a | NOT_TRIGGERABLE: same /compact result as PreCompact. |
| PreModelSwitch | no | - | none | n/a | /model haiku on an already-haiku session did not switch. /model sonnet did switch: the stream shows PreModelSwitch:claude-sonnet-5 with empty stdout, but this plugin's jsonl did not get that row (the async hook exited before the log flush). PostModelSwitch was logged. |
| PostModelSwitch | yes | cache_ttl, context_tokens, cwd, estimated_cache_write_usd, from_model, hook_event_name, pricing, prompt_cache_warm, prompt_id, requested_model, session_id, source, to_model, transcript_path | none | n/a | - |
| Elicitation | yes | cwd, hook_event_name, mcp_server_name, message, mode, prompt_id, requested_schema, session_id, transcript_path | none | n/a | - |
| ElicitationResult | yes | action, cwd, hook_event_name, mcp_server_name, mode, prompt_id, session_id, transcript_path | none | n/a | - |
| SessionEnd | yes | cwd, hook_event_name, prompt_id, reason, session_id, transcript_path | none | n/a | - |

## Write in claude -p

| id | setup | PermissionRequest | file written | exit | errors |
| --- | --- | --- | --- | --- | --- |
| write-a | no hook decision, default permission mode | yes | no | 0 | {"type": "system", "subtype": "permission_denied", "tool_name": "Write", "decision_reason": "Claude requested permissions to edit /Users/timon/Sync/Test/cc-cursor-bridge/examples/sandbox/hook-harness/run-20261021-193741/write-a.txt which is a sensitive file.", "decision_reason_type": "safetyCheck", "message": "Claude requested permissions to edit /Users/timon/Sync/Test/cc-cursor-bridge/examples/sa…[truncated]; Claude requested permissions to edit /Users/timon/Sync/Test/cc-cursor-bridge/examples/ |
| write-b | PreToolUse permissionDecision allow | yes | no | 0 | {"type": "system", "subtype": "permission_denied", "tool_name": "Write", "decision_reason": "Claude requested permissions to edit /Users/timon/Sync/Test/cc-cursor-bridge/examples/sandbox/hook-harness/run-20261021-193741/write-b.txt which is a sensitive file.", "decision_reason_type": "safetyCheck", "message": "Claude requested permissions to edit /Users/timon/Sync/Test/cc-cursor-bridge/examples/sa…[truncated]; Claude requested permissions to edit /Users/timon/Sync/Test/cc-cursor-bridge/examples/ |
| write-c | PermissionRequest decision allow | yes | yes | 0 | - |
| write-d | --permission-mode acceptEdits | yes | no | 0 | {"type": "system", "subtype": "permission_denied", "tool_name": "Write", "decision_reason": "Claude requested permissions to edit /Users/timon/Sync/Test/cc-cursor-bridge/examples/sandbox/hook-harness/run-20261021-193741/write-d.txt which is a sensitive file.", "decision_reason_type": "safetyCheck", "message": "Claude requested permissions to edit /Users/timon/Sync/Test/cc-cursor-bridge/examples/sa…[truncated]; Claude requested permissions to edit /Users/timon/Sync/Test/cc-cursor-bridge/examples/ |
| write-e | --allowedTools Write | yes | no | 0 | {"type": "system", "subtype": "permission_denied", "tool_name": "Write", "decision_reason": "Claude requested permissions to edit /Users/timon/Sync/Test/cc-cursor-bridge/examples/sandbox/hook-harness/run-20261021-193741/write-e.txt which is a sensitive file.", "decision_reason_type": "safetyCheck", "message": "Claude requested permissions to edit /Users/timon/Sync/Test/cc-cursor-bridge/examples/sa…[truncated]; Claude requested permissions to edit /Users/timon/Sync/Test/cc-cursor-bridge/examples/ |
| write-f | --permission-prompt-tool mcp__permit__approve | yes | yes | 0 | - |

PreToolUse deny probe (echo hi): honoured, Bash did not reach PostToolUse
