---
name: commit
description: 把与当前任务相关的改动提交为一次 git commit（不推送）
allowed_tools: [execute_command, read_file]
argument-hint: "[对提交说明的要求]"
---

请把当前仓库中与本次任务相关的改动提交为一次 git commit。

1. 用 git status 和 git diff HEAD 查看改动；不是 git 仓库或没有可提交的改动时，直接说明并结束。
2. 用 git log -n 5 --oneline 查看最近的提交说明，沿用仓库的语言和格式。
3. 只暂存与本次任务相关的文件，用 git add <路径> 逐个列出；不要用 git add -A 或 git add .。看起来含密钥或凭据的文件（如 .env）不要暂存，并提醒用户。
4. 写一条说明改动目的的提交说明，执行 git commit。不要 --amend，不要 --no-verify，不要推送。
5. 提交钩子失败时报告原因；修复后重新提交，不改写已有提交。
6. 完成后用 git status 和 git log -n 1 确认结果并汇报。
