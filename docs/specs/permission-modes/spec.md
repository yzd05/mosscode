# 四档权限与计划模式联动

日期：2026-10-15。按用户提供的表格实施；已确认 plan 的写入/命令从直接禁止改为可申请审批。

| 模式 | 只读 | 专用文件写入/编辑 | Shell 命令 |
| --- | --- | --- | --- |
| default | Allow | Ask | Ask |
| acceptEdits | Allow | Allow | Ask |
| plan | Allow | Ask | Ask |
| bypassPermissions | Allow | Allow | Allow |

表格是无匹配规则时的行为。原有硬拦截、文件路径限制、配置文件保护、会话 > 本地 > 项目 > 用户的规则顺序和同层 deny 优先继续生效。明确 allow 可免去默认 Ask。任何模式都不能解除硬检查或最终生效的 deny。

- Shell 始终属于命令类别，不能因为命令内容是写文件而获得 acceptEdits 的自动授权。
- MCP 在 default/acceptEdits 下 Ask，在 bypassPermissions 下 Allow，plan 仍保留此前禁用 MCP 的边界；不凭 readOnlyHint 放行。
- /permissions 的补全和说明展示四个新模式，状态用中文区分。旧 permissive 精确兼容为 bypassPermissions。旧 strict 仅作为兼容入口保留逐次确认，避免旧配置静默降低权限，不列入新模式菜单。（阶段 3b 已移除这两个旧名，改为报错提示替代档位，见 [refinement-03b-permissions](../refinement-03b-permissions/spec.md)。）
- /plan 切换权限为 plan，并记住进入前的权限；/do 恢复进入前的权限（直接以 plan 启动则恢复 default）。/permissions plan 同样进入规划行为，切换其他权限同样退出 plan，不出现两个模式字段不一致。
- 计划模式先调查并给出计划，模型仍可看见六个本地工具；写入与命令须通过权限入口决定。MCP 不提供给计划模式。批准工具后仍保持计划模式，不自动切成 bypassPermissions。
- CLI、YAML、无界面、TUI 共用同一策略。无界面的 Ask 返回 approval_required，不自动授权。
- 不修改用户现有配置，不增加 OS 沙箱或扩大权限文件保护的承诺；已知 -Force 正则误判不在本次模式调整中顺带改变。

实现集中在 permissions.py；AgentLoop 从同一策略读取模式，更新工具清单与提示；Conversation 提供切换入口；CLI/TUI/补全帮助同步。

验收：四档与所有工具类别矩阵；acceptEdits 的 Bash 写入审批与专用文件自动写入；plan 的审批允许/拒绝与结果回写；/plan、/do、/permissions 联动及启动 plan；分层规则和硬检查不变；MCP 分类、CLI/YAML 与旧模式兼容；全量回归。
