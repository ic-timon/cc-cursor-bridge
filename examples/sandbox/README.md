# sandbox

`claude --plugin-dir` 自测用的临时工作目录。这里的运行产物（模型写的文件、事件日志、debug 日志）被仓库根目录的 `.gitignore` 忽略，不要提交。

自测命令见仓库 `README.md`。带 `-p` 时 prompt 从 stdin 送入：`scripts/run_claude_bridged.sh --prompt-file prompt.txt`，或 `claude -p --plugin-dir <repo> < prompt.txt`。不要把说明文字放在 `--allowedTools` 后面。
