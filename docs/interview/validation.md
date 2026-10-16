# 项目讲解 HTML 验证

日期：2026-10-12。文件：[mosscode-project-guide.html](mosscode-project-guide.html)。本次仅新增讲解文档，不改变运行时代码或权限配置。

## 内容依据

- 项目 README 与第六章 spec.md、validation.md。
- 当前 security.py、permissions.py、tools/base.py、tools/execution.py、tools/files.py 与 search_worker.py 的实现。
- Python 3.12 pathlib 官方文档、Microsoft Job Objects 文档及 MITRE CWE-367，链接放在对应解释附近。
- 第六章的 242 passed / 1 skipped 为此前记录的全项目验收成绩，本次没有重新运行全部测试，也没有调用真实 API。

## 本页检查

- HTML 标识符无重复，37 个 ID；31 个项目链接与页内锚点均有对应目标。
- JavaScript 语法检查通过。
- 10 种规则情境 × 3 个权限档位 × 2 个任务模式 × 2 个入口，共 120 种演示决策与当前 PermissionPolicy.operation / decide 的源码行为对比一致；计划模式入口拦截与无界面 ask 转 approval_required 按实际执行流程对比。
- 6 个固定路径样例、讲解稿选项切换、选择控件 change 事件通过脚本级检查。路径演示仅展示预置案例，不是另一个文件系统解析器。
- 审批 PNG 已内嵌，CSS 和 JavaScript 均在 HTML 内，无 CDN 或外部运行依赖。源码链接仍依赖原项目目录。

## 预览限制

浏览器工具的 URL 安全策略拒绝 file 协议，未通过其他方式绕过。已用 Codex 文件打开工具请求打开文档，返回 queued；这不构成浏览器页面已显示的证据。本次未完成桌面、窄屏或打印的浏览器视觉验收，亦未声称完成。
