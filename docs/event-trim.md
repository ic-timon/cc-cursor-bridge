# wait_all 事件裁剪建议

还没改运行行为。表里的「fizzbuzz pending」来自现场 demo `0232cdaf22cd4acb881416b8ab371d65`（`/private/tmp/ccb-demo-fFSz`），那次一共 24 个 pending。`-p` 是否触发以 `tests/hooks/report.md` 为准。

模式：`WAIT` 必须等 agent 回复；`PUSH` 只异步通知、不阻塞；`LOG` 只写本地日志；`OFF` 不注册。这次没有建议 `OFF`。

| 事件 | 决定权 | -p 里触发 | fizzbuzz pending | 建议 | 原因 |
| --- | --- | --- | --- | --- | --- |
| SessionStart | additionalContext、watchPaths | yes | 1 | PUSH | 能注入上下文，但 demo 只 ack，等待会卡住整个会话启动。 |
| Setup | 无 | yes | 0 | LOG | 只在 `--init` / `--maintenance` 出现，没有可回复的决定。 |
| InstructionsLoaded | 无 | yes | 0 | LOG | 观察 CLAUDE.md 被加载，不能阻断。 |
| UserPromptSubmit | block、additionalContext | yes | 1 | PUSH | 可以拦提示，demo 只 ack，每轮都等会拖慢整轮。 |
| UserPromptExpansion | block | yes | 0 | PUSH | 可以拦斜杠命令展开，fizzbuzz 没碰到。 |
| MessageDisplay | displayContent | yes | 5 | LOG | 只改屏幕文本，demo 里出现 5 次且都是 ack。 |
| PreToolUse | allow / deny / ask / defer、updatedInput | yes | 6 | WAIT | 工具放行的主门；规则命中则直接 deny、不写 pending。 |
| PermissionRequest | allow / deny、updatedInput | yes | 0 | WAIT | 仓库内 Write 的 safetyCheck 只有这里的 allow 能放行。 |
| PermissionDenied | 无（已拒绝） | no | 0 | LOG | 拒绝已经发生，等回复改变不了这次调用。 |
| PostToolUse | additionalContext、block | yes | 3 | PUSH | 能补上下文，demo 只 ack，不必挡住下一步。 |
| PostToolUseFailure | additionalContext | yes | 1 | PUSH | 失败值得告诉 agent，不必为了 ack 卡住。 |
| PostToolBatch | 无实质决定 | yes | 5 | LOG | 一批工具结束的回声，demo 里 5 次全是 ack。 |
| Notification | 无 | yes | 0 | LOG | 本身就是通知，再等一次没有新决定。 |
| SubagentStart | 无 | yes | 0 | LOG | 子代理已经启动，hook 不能取消它。 |
| SubagentStop | block + reason | yes | 0 | PUSH | 可以拦住子代理结束，fizzbuzz 没用到这个决定。 |
| TaskCreated | block | yes | 0 | PUSH | 可以拦建任务，普通编码任务里很少需要当场否决。 |
| TaskCompleted | block | yes | 0 | PUSH | 同上，完成标记不必每次等人。 |
| Stop | block + reason | yes | 1 | PUSH | 可以要求继续，demo 只 ack，等待会拖住进程退出。 |
| StopFailure | 无 | no | 0 | PUSH | API 失败要让 agent 看见，但这次调用已经结束，不必再等。 |
| TeammateIdle | block | no | 0 | PUSH | 可以拦住队友空闲；当前 `-p` 触发不了，先不要同步等待。 |
| ConfigChange | 无 | yes | 0 | LOG | 配置变了，hook 不能撤销这次变更。 |
| CwdChanged | 无 | no | 0 | LOG | 目录已经变了；当前无头跑法也触发不了。 |
| DirectoryAdded | 无 | no | 0 | LOG | 目录已经加进会话；`-p` 里 `/add-dir` 不可用。 |
| FileChanged | watchPaths | yes | 0 | LOG | 文件已经改完，等回复不能撤销写入。 |
| WorktreeCreate | 返回 worktree 路径 | yes | 0 | PUSH | hook 已经建好 git worktree，再等路径只会拖启动。 |
| WorktreeRemove | 退出码表示是否删掉 | yes | 0 | LOG | 只在 ExitWorktree 时出现，删除本身不需要 agent 当场选。 |
| PreCompact | block | yes | 0 | PUSH | 可以拦住压缩，普通任务不该为它停一轮。 |
| PostCompact | 无 | yes | 0 | LOG | 压缩已经完成。 |
| PreModelSwitch | allow / deny / ask | yes | 0 | PUSH | 可以拦住换模型，fizzbuzz 不需要每次确认。 |
| PostModelSwitch | 无 | yes | 0 | LOG | 模型已经换完。 |
| Elicitation | accept / decline | yes | 0 | WAIT | 这是在替用户回答 MCP 提问，必须有人决定。 |
| ElicitationResult | accept / decline | yes | 0 | LOG | 结果已经产生，再等一次是重复。 |
| SessionEnd | 无 | yes | 1 | LOG | 进程正在退出，官方预算最多约 60 秒，不该再等人。 |
