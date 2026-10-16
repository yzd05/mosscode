# 第七章 MCP 验证记录

日期：2026-10-14。环境：Windows、Python 3.12、项目 `.venv`；官方 `mcp==1.30.0`、`jsonschema==4.26.0`。当前环境已安装新依赖并更新 editable 包元数据，完整版本见 `requirements-lock.txt`。

## 结果

| 检查 | 结果 |
| --- | --- |
| MCP 专项 `tests/test_mcp.py` | 23 通过 |
| 全量回归（退出连接期间的最后一个新增测试加入前） | 264 通过，1 跳过 |
| 新增启动期间退出测试 | 已包含在最终 23 项 MCP 专项中，通过 |
| `compileall -q src scripts tests` | 通过 |
| `pip check`（更新 editable 包后） | No broken requirements found |
| Windows 真实控制台自动烟测 | 通过；含 MCP 发现、键盘审批、调用、清理及终端恢复 |

跳过项是当前 Windows 用户无法创建文件符号链接的既有权限测试；目录 junction 检查及其他权限测试通过。没有把跳过项算作成功。

首次全量回归已通过。后来同时运行安装、控制台烟测和回归时，3 个既有命令进程时限测试超时；结束并行进程后顺序重跑全量得到上表结果，没有放宽产品超时或测试断言。新控制台 MCP 模拟 Provider 的缺失消息参数也已修正，并在未重定向的真实控制台重新通过。

## 可复核命令

```powershell
New-Item -ItemType Directory -Path .test-artifacts -Force | Out-Null
.\.venv\Scripts\python.exe -m pytest -q tests/test_mcp.py --basetemp .test-artifacts\mcp-20261014-08
.\.venv\Scripts\python.exe -m pytest -q --basetemp .test-artifacts\full-20261014-03
.\.venv\Scripts\python.exe -m compileall -q src scripts tests
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe scripts\verify_inline_terminal.py
```

`--basetemp` 使用项目内专用目录：本机系统临时目录中已有 pytest 根目录访问被拒绝，未修改或删除它。复核时可换一个未使用的项目内目录。

## 验收覆盖

- 用户/项目配置整项覆盖；env/headers 展开；缺少变量、凭据 URL、非法类型/字段、无效 YAML；错误不回显假凭据。
- 实际启动本地 stdio 子进程，官方 SDK initialize/list/call；列表分页、env 传入、复用、并行请求乱序响应与 ID 关联。
- 本地 HTTP Server 的 JSON/SSE POST、headers、Session ID、DELETE 清理；不建立 GET 通知流，中断带 event ID 的 SSE 不恢复或重发。
- 原始 JSON Schema、内部引用、非法参数在请求前拦截、拒绝外部引用；引用注册表未配置网络获取。
- isError、structuredContent、未展示图片类型、错误正文截断、协议错误、断连、超时、取消与断连后的不可用结果。
- 单个 Server 初始化失败、重复分页、HTTP 503、启动超时不影响正常 Server；重复/并发 start、重复 close、关闭后不重启和初始化取消。
- 忽略 readOnlyHint 的保守分类、计划模式不展示/执行、默认审批、JSON 大小写敏感、永久精确授权重载与规则拒绝；已有规则层级和模式语义全量回归。
- Agent Loop 保存并续用工具结果；审批取消配齐历史；TUI 启动发现、键盘授权、下方审批、退出清理；无界面需确认返回 approval_required。
- 专用本地工具保护用户/项目 MCP 配置；MCP 工具不使用本地路径校验，不宣称隔离 Server 实际访问。

控制台烟测同时检查原生背景、无 alternate screen、应用不清屏、鼠标报告关闭、跨启动原生滚动历史、命令补全、思考/流式/Markdown、计划模式、本地工具和权限面板。MCP 工具在真实驱动中经审批执行，退出时生命周期任务完成、stdio EOF 清理标记存在，控制台模式恢复。

## 验证边界

测试 Server 和模型均为本地固定模拟对象，未读取真实模型凭据，未连接用户实际 MCP Server。真实远程服务、OAuth、Server 自身权限隔离及网络策略尚未验收；本章不实现这些能力。取消或断连后可能存在外部效果，不承诺回滚。

HTTP transport 复用官方 SDK，通过固定 1.30.x 的一个恢复钩子关闭 SSE 自动恢复；版本升级需要重新运行断流及生命周期测试。配置和工具说明见根目录 README 与 mcp.example.yaml。
