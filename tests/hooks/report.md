# Hook 实测摘要

2026-10-21，Claude Code 2.1.306。`tests/hooks/run_all.py` 和 `tests/hooks/run_gap.py` 的完整输出写到 `tests/hooks/out/`，不入库。这里只留结论。触发 28/33。

| 事件 | 触发 |
| --- | --- |
| SessionStart | yes |
| Setup | yes |
| InstructionsLoaded | yes |
| UserPromptSubmit | yes |
| UserPromptExpansion | yes |
| MessageDisplay | yes |
| PreToolUse | yes |
| PermissionRequest | yes |
| PermissionDenied | no |
| PostToolUse | yes |
| PostToolUseFailure | yes |
| PostToolBatch | yes |
| Notification | yes |
| SubagentStart | yes |
| SubagentStop | yes |
| TaskCreated | yes |
| TaskCompleted | yes |
| Stop | yes |
| StopFailure | no |
| TeammateIdle | no |
| ConfigChange | yes |
| CwdChanged | no |
| DirectoryAdded | no |
| FileChanged | yes |
| WorktreeCreate | yes |
| WorktreeRemove | yes |
| PreCompact | yes |
| PostCompact | yes |
| PreModelSwitch | yes |
| PostModelSwitch | yes |
| Elicitation | yes |
| ElicitationResult | yes |
| SessionEnd | yes |

## 未能触发

- PermissionDenied：`--permission-mode auto` 配 sonnet，允许 Bash 时 `curl` 被执行而不是拒绝。不带 allowedTools 的 `sudo true` 在 90 秒超时里仍停在思考，只有 SessionStart 和 UserPromptSubmit。haiku 不支持 auto mode。
- StopFailure：把这一次进程的 API 地址指到本机未监听端口，并换一个无效模型名，请求仍经本机代理返回成功。没有改 cc-switch。
- TeammateIdle：`claude --agent-teams` 报 unknown option。只设 `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` 也没有队友空闲事件。
- CwdChanged：Bash `cd` 和 EnterWorktree 都没有改会话工作目录。前者只跑了命令，后者发出的是 WorktreeCreate。
- DirectoryAdded：启动时的 `--add-dir` 不发这个事件。会话里的 `/add-dir` 返回当前环境不可用。

## Write 与 -p

仓库路径里的 Write 会被 Claude Code 的 safetyCheck 当成敏感文件。仓库外的同一条 Write 不需要额外权限就能写成。

| id | 设置 | PermissionRequest | 文件写成 |
| --- | --- | --- | --- |
| write-a | 无 hook 决定，默认权限 | yes | no |
| write-b | PreToolUse allow | yes | no |
| write-c | PermissionRequest allow | yes | yes |
| write-d | `--permission-mode acceptEdits` | yes | no |
| write-e | `--allowedTools Write` | yes | no |
| write-f | `--permission-prompt-tool` | yes | yes |

PreToolUse allow 挡不住这次 safetyCheck。PermissionRequest allow 和 permission-prompt-tool 能把文件写下来。
