---
name: init
description: 分析当前项目，生成或补充给 coding agent 看的项目说明 AGENTS.md
allowed_tools: [read_file, glob, grep, execute_command, write_file, edit_file]
argument-hint: "[侧重点]"
---

请为当前项目生成或补充 AGENTS.md：写给在本仓库中工作的 coding agent 看的项目说明，MossCode 每次启动都会把它放进上下文。

1. 先看已有的说明：当前目录的 AGENTS.md、CLAUDE.md、.mosscode/AGENTS.md，以及 README、CONTRIBUTING 等文档。
2. 了解项目：用 glob 查看目录结构，读取构建与依赖配置（如 pyproject.toml、package.json、Cargo.toml、go.mod、Makefile、CI 配置），找出安装、构建、运行测试（包括只运行单个测试）、代码检查和格式化的命令。只运行只读的命令（如 git log -n 20 --oneline、git ls-files）；不要安装依赖，不要运行构建或测试。
3. 只写仅读几个文件看不出来、但在本仓库工作必须知道的内容：常用命令、主要模块的职责和入口、代码风格与命名约定、测试约定、容易出错的地方。不要罗列每个文件，不要写通用的编程建议，不要编造没有依据的命令或约定，拿不准的不写。尽量简短，通常不超过 100 行。
4. 写入：
   - 已有 AGENTS.md 时，用 edit_file 补充缺少或已过时的部分，保留原有内容和结构，不要重写整个文件；
   - 只有 CLAUDE.md 时在其中补充（同一目录没有 AGENTS.md 时 MossCode 读取 CLAUDE.md），不另建 AGENTS.md；
   - 都没有时用 write_file 新建 AGENTS.md，使用项目文档的语言。
5. 完成后简要说明写了哪些内容、依据是什么，以及哪些地方需要用户自己补充或确认。
