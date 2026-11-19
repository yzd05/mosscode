"""Built-in tools for discovering and reading MCP resources and URI templates."""

from pathlib import Path
from typing import Any

from pydantic import Field

from ..mcp_client import MCPManager, MCPResourceInfo, MCPResourceTemplateInfo
from ..permissions import Operation, PermissionPolicy
from .base import CURRENT_REGISTRY, IMAGES, Params, Tool, ToolFailure, image_context


class ListMCPResourcesParams(Params):
    server: str | None = Field(default=None, max_length=128)
    include_templates: bool = True


class ReadMCPResourceParams(Params):
    server: str = Field(min_length=1, max_length=128)
    uri: str = Field(min_length=1, max_length=2048)


def _resource_denied(policy: PermissionPolicy | None, server: str, target_uri: str) -> bool:
    if policy is None:
        return False
    srv = server.strip()
    uri = target_uri.strip()
    if policy.decide(Operation("read_mcp_resource", srv, server=srv), read_only=True).action == "deny":
        return True
    if (
        policy.decide(
            Operation("read_mcp_resource", f"{srv}:{uri}", server=srv),
            read_only=True,
        ).action
        == "deny"
    ):
        return True
    return policy.decide(Operation("list_mcp_resources", srv, server=srv), read_only=True).action == "deny"


def _format_resource(item: MCPResourceInfo) -> str:
    parts = [f"- [{item.server}] {item.uri}"]
    if item.name and item.name != item.uri:
        parts.append(f"({item.name})")
    if item.mime_type:
        parts.append(f"[{item.mime_type}]")
    summary = item.description or item.title
    if summary:
        parts.append(f"— {summary}")
    return " ".join(parts)


def _format_template(item: MCPResourceTemplateInfo) -> str:
    parts = [f"- [{item.server}] {item.uri_template}"]
    if item.name and item.name != item.uri_template:
        parts.append(f"({item.name})")
    if item.mime_type:
        parts.append(f"[{item.mime_type}]")
    summary = item.description or item.title
    if summary:
        parts.append(f"— {summary}")
    return " ".join(parts)


class ListMCPResourcesTool(Tool):
    name = "list_mcp_resources"
    read_only = True
    plan_allowed = True
    permission_kind = "mcp_resource"
    description = (
        "列出当前已连接 MCP Server 提供的资源（Resource）与资源 URI 模板（Resource Template）。"
        "可通过 server 参数筛选特定 MCP Server，通过 include_templates 控制是否包含 URI 模板。"
    )
    prompt_summary = "列出已就绪 MCP Server 的资源与 URI 模板"
    prompt_guidelines = (
        "需要读取外部 MCP Server 暴露的文档、数据库记录或上下文资源时，先用 list_mcp_resources 查看可用 URI 或模板，再用 read_mcp_resource 读取。",
    )
    params_model = ListMCPResourcesParams

    def __init__(self, root: Path, manager: MCPManager, permissions: PermissionPolicy | None = None):
        super().__init__(root)
        self.manager = manager
        self.permissions = permissions

    def _active_policy(self) -> PermissionPolicy | None:
        registry = CURRENT_REGISTRY.get()
        if registry is not None and getattr(registry, "permissions", None) is not None:
            return registry.permissions
        return self.permissions

    async def execute(self, params: ListMCPResourcesParams) -> tuple[str, dict[str, Any]]:
        policy = self._active_policy()
        server = (params.server or "").strip() or None
        if server is not None and _resource_denied(policy, server, "*"):
            return "未发现可用的 MCP 资源。", {"count": 0, "resources": [], "resource_templates": []}

        resources = [
            item for item in self.manager.list_resources(server) if not _resource_denied(policy, item.server, item.uri)
        ]
        templates = (
            [
                item
                for item in self.manager.list_resource_templates(server)
                if not _resource_denied(policy, item.server, item.uri_template)
            ]
            if params.include_templates
            else []
        )

        sections: list[str] = []
        if resources:
            sections.append(f"MCP 资源（{len(resources)} 项）：\n" + "\n".join(_format_resource(r) for r in resources))
        if templates:
            sections.append(
                f"MCP 资源模板（{len(templates)} 项）：\n" + "\n".join(_format_template(t) for t in templates)
            )
        output = "\n\n".join(sections) if sections else "未发现可用的 MCP 资源。"
        details = {
            "count": len(resources) + len(templates),
            "resources": [
                {
                    "server": r.server,
                    "uri": r.uri,
                    "name": r.name,
                    "mime_type": r.mime_type,
                    "description": r.description,
                }
                for r in resources
            ],
            "resource_templates": [
                {
                    "server": t.server,
                    "uri_template": t.uri_template,
                    "name": t.name,
                    "mime_type": t.mime_type,
                    "description": t.description,
                }
                for t in templates
            ],
        }
        return output, details


class ReadMCPResourceTool(Tool):
    name = "read_mcp_resource"
    read_only = True
    plan_allowed = True
    permission_kind = "mcp_resource"
    timeout = 600.0  # Each ServerConnection enforces its own configured timeout.
    description = "按 server 名称和资源 uri 读取已连接 MCP Server 的资源内容（支持文本、图片及二进制摘要）。"
    prompt_summary = "读取指定 MCP Server 的资源"
    params_model = ReadMCPResourceParams

    def __init__(self, root: Path, manager: MCPManager, permissions: PermissionPolicy | None = None):
        super().__init__(root)
        self.manager = manager
        self.permissions = permissions

    async def execute(self, params: ReadMCPResourceParams) -> tuple[str, dict[str, Any]]:
        server = params.server.strip()
        raw_uri = params.uri.strip()
        if not server or not raw_uri:
            raise ToolFailure("invalid_arguments", "MCP Server 名称与资源 URI 不能为空。", executed=False)
        registry = CURRENT_REGISTRY.get()
        if registry is None and self.permissions is not None:
            decision = self.permissions.decide(
                Operation(self.name, f"{server}:{raw_uri}", server=server),
                read_only=True,
            )
            if decision.action == "deny":
                raise ToolFailure(decision.code or "permission_denied", decision.reason, executed=False)
        accepts, store = image_context()
        output, refs, details = await self.manager.read_resource(
            server,
            raw_uri,
            accepts_images=accepts,
            image_store=store,
        )
        out_details = dict(details)
        if refs:
            out_details[IMAGES] = refs
        return output, out_details
