---
name: test
description: 检测项目类型并运行测试，失败时分析原因
allowed_tools: [detect, execute_command, read_file, glob, grep]
argument-hint: "[测试范围]"
---

请运行本项目的测试。

1. 先调用 skill__test__detect 获取建议的测试命令；结果为空时，查看 README、AGENTS.md 等项目说明找测试方式，仍找不到就说明并结束。
2. 有补充要求时（如只测某个文件或用例）按要求缩小范围。
3. 运行测试命令。
4. 全部通过时汇报结果；有失败时阅读失败用例和相关代码，说明原因和修改建议。用户没有要求修复时不修改文件。
