---
name: review
description: 审查当前仓库中尚未提交的改动（只读），按严重程度列出问题
mode: fork
fork_context: none
allowed_tools: [read_file, glob, grep, execute_command]
argument-hint: "[范围或侧重点]"
---

请审查当前仓库中尚未提交的改动。

1. 先用 git status 和 git diff HEAD 查看改动范围；不是 git 仓库或没有改动时，直接说明并结束。
2. 阅读改动涉及的上下文代码，找出正确性问题、边界条件、错误处理、安全隐患，以及与周边代码约定不一致的地方。
3. 只审查，不修改文件，不运行会改变仓库或环境状态的命令。
4. 按严重程度从高到低列出问题，每条写出 文件:行号、问题、触发场景和修改建议；没有发现问题时明确说明。
