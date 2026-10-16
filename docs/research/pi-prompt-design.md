# Pi 提示词设计与 MossCode 的调整

调查日期：2026-10-11。通过 Context7 官方仓库索引 `/earendil-works/pi` 查询提示组装，再对照官方 `main` 源码；以下链接可能随后更新，不代表固定发布版本。

Pi 的构建器按启用工具收集摘要和使用规则，去重规则，并提供命名段落与自定义提示接口。跨工具指导留在构建器，具体能力指导由工具定义贡献。[system-prompt.ts](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/src/core/system-prompt.ts)

例如，读取工具贡献“优先通过读取工具查看文件”的规则，写入工具贡献适用范围，编辑工具贡献原文上下文的指导。这些规则与实际工具能力一起维护。[read.ts](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/src/core/tools/read.ts)、[write.ts](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/src/core/tools/write.ts)、[edit.ts](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/src/core/tools/edit.ts)

据此对 MossCode 做以下调整：

- 保留用户指定的七模块顺序，扩充调查、修改、恢复和验证的具体指导，取消任意字符上限。
- 工具各自维护 `prompt_summary` 和 `prompt_guidelines`，按实际启用列表生成能力清单，规则去重。指导同时进入 API 描述，元信息字段本身不发送给 API。
- 计划模式只贡献只读工具；没有工具时说明能力限制。模式切换可以改变稳定工具前缀，同一工具集的循环中不混入日期、路径和轮次。
- 环境和模式提醒沿用第五章的独立系统补充通道。自定义 `system_prompt` 仍完整替换默认提示；工具 API 描述和模式执行限制仍有效。

以上是适合当前章节的借鉴。MossCode 仍是单片段唯一匹配编辑、UTF-8 文本读取；未增加 Pi 的批量编辑、图片读取、项目资源、Skill 加载或命名段落增量协议。完整默认示例见 [default-prompt.md](../specs/chapter-05-prompts/default-prompt.md)。本地测试只能确认组装和协议行为；模型任务质量变化仍需按 [人工场景](../specs/chapter-05-prompts/manual-evaluation.md) 实际比较。
