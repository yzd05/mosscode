"""ACP URI parsing, client MCP server conversion, and prompt content block unpacking."""

from __future__ import annotations

import base64
import os
import re
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit
from urllib.request import url2pathname

from acp.schema import (
    AcpMcpServer,
    AudioContentBlock,
    BlobResourceContents,
    EmbeddedResourceContentBlock,
    HttpMcpServer,
    ImageContentBlock,
    McpServerStdio,
    ResourceContentBlock,
    SseMcpServer,
    TextContentBlock,
    TextResourceContents,
)

from ..images import ImageError, ProcessedImage, load_image
from ..mcp_config import SERVER_NAME, ServerConfig
from ..mentions import quoted
from ..messages import image_mark
from ..permissions import Operation
from ..sandbox import HOME_SECRETS
from ..security import inside
from .events import display_path

if TYPE_CHECKING:
    from .session import AcpSession

McpServerParam = HttpMcpServer | SseMcpServer | AcpMcpServer | McpServerStdio
PromptBlock = (
    TextContentBlock | ImageContentBlock | AudioContentBlock | ResourceContentBlock | EmbeddedResourceContentBlock
)


def _is_file_uri(uri: str | None) -> bool:
    return bool(uri) and uri.lstrip()[:5].lower() == "file:"


def _uri_paths(uri: str) -> tuple[Path, Path] | None:
    """Parse a file: URI into (lexical_abspath, resolved_path), or None when non-file or invalid."""
    if not _is_file_uri(uri):
        return None
    stripped = uri.lstrip()
    if "\0" in stripped:
        return None
    try:
        parsed = urlsplit(stripped)
    except ValueError:
        return None
    if parsed.scheme.lower() != "file":
        return None
    netloc = parsed.netloc
    if netloc and netloc.lower() != "localhost":
        if os.name == "nt":
            if re.fullmatch(r"[A-Za-z]:", netloc) and parsed.path.startswith("/"):
                url_path = f"/{netloc}{parsed.path}"
            elif parsed.path.startswith("/"):
                url_path = f"//{netloc}{parsed.path}"
            else:
                return None
        else:
            return None
    else:
        if not parsed.path.startswith("/"):
            return None
        url_path = parsed.path
    try:
        raw_path = url2pathname(url_path)
    except (ValueError, OSError):
        return None
    if not raw_path or "\0" in raw_path:
        return None
    try:
        lexical = Path(os.path.abspath(raw_path))
        resolved = lexical.resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    return lexical, resolved


def _uri_to_path(uri: str) -> Path | None:
    pair = _uri_paths(uri)
    return pair[1] if pair is not None else None


def convert_client_mcp_servers(
    mcp_servers: Sequence[McpServerParam] | None,
) -> tuple[dict[str, ServerConfig], list[str]]:
    """Convert ACP client mcpServers definitions into MossCode ServerConfig objects."""
    servers: dict[str, ServerConfig] = {}
    notices: list[str] = []
    for item in mcp_servers or ():
        if not re.fullmatch(SERVER_NAME, item.name):
            notices.append(f"客户端 MCP 服务器名称 {item.name!r} 不合法，已跳过。")
            continue
        if isinstance(item, McpServerStdio):
            if not item.command or "\0" in item.command:
                notices.append(f"MCP {item.name} 的 command 不合法，已跳过。")
                continue
            env = {var.name: var.value for var in item.env if var.name and "\0" not in var.value}
            servers[item.name] = ServerConfig(
                name=item.name,
                type="stdio",
                command=item.command,
                args=tuple(item.args),
                env=env,
            )
        elif isinstance(item, HttpMcpServer):
            parsed = urlsplit(item.url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                notices.append(f"MCP {item.name} 的 URL 不合法，已跳过。")
                continue
            headers = {h.name: h.value for h in item.headers}
            servers[item.name] = ServerConfig(
                name=item.name,
                type="http",
                url=item.url,
                headers=headers,
            )
        elif isinstance(item, (SseMcpServer, AcpMcpServer)):
            kind = getattr(item, "type", "unknown")
            notices.append(f"MCP 服务器 {item.name} 使用了暂不支持的 {kind} 传输协议，已跳过。")
    return servers, notices


def _decode_image_block(data_b64: str, mime_type: str) -> ProcessedImage | None:
    try:
        raw = base64.b64decode(data_b64, validate=True)
        return load_image(raw)
    except (ImageError, ValueError, Exception):
        return None


def refused_resource_reason(session: AcpSession, lexical: Path, resolved: Path | None = None) -> str | None:
    if resolved is None:
        try:
            resolved = lexical.resolve()
        except OSError:
            return "路径解析失败"
    candidates = (lexical, resolved) if lexical != resolved else (lexical,)
    raw_home = (session.options.home or Path.home()).expanduser()
    home_lex = Path(os.path.abspath(raw_home))
    try:
        home_res = home_lex.resolve()
    except OSError:
        home_res = home_lex

    for secret in HOME_SECRETS:
        secret_lex = Path(os.path.abspath(home_lex / secret))
        try:
            secret_res = (home_res / secret).resolve()
        except OSError:
            secret_res = secret_lex
        for cand in candidates:
            if inside(secret_lex, cand) or inside(secret_res, cand):
                return f"受保护的凭据路径 {secret_lex.name} 未附加"

    if session.settings.source is not None:
        src_lex = Path(os.path.abspath(session.settings.source))
        try:
            src_res = src_lex.resolve()
        except OSError:
            src_res = src_lex
        for cand in candidates:
            if inside(src_lex, cand) or inside(src_res, cand):
                return "受保护的配置文件未附加"

    registry = session.conversation.registry
    policy = session.policy
    if registry is not None:
        read_tool = registry.get("read_file")
        if read_tool is not None:
            for cand in candidates:
                try:
                    params = read_tool.validate_arguments({"path": str(cand)})
                    refusal = registry.refusal(read_tool, params)
                except Exception:
                    return "路径校验未通过"
                if refusal is not None:
                    return refusal.reason
                if policy is not None:
                    outside = not inside(session.cwd, cand)
                    target = cand.as_posix() if outside else cand.relative_to(session.cwd).as_posix()
                    decision = policy.decide(Operation("read_file", target, outside=outside), read_only=True)
                    if decision.action == "deny":
                        return decision.reason
    return None


def unpack_prompt_blocks_parts(
    session: AcpSession,
    blocks: Sequence[PromptBlock],
) -> tuple[str, list[str], str, dict[str, ProcessedImage]]:
    user_parts: list[str] = []
    context_parts: list[str] = []
    ordered_parts: list[str] = []
    images: dict[str, ProcessedImage] = {}
    img_counter = 0

    def _add_context(item: str) -> None:
        context_parts.append(item)
        ordered_parts.append(item)

    for block in blocks:
        if isinstance(block, TextContentBlock):
            if block.text:
                user_parts.append(block.text)
                ordered_parts.append(block.text)
        elif isinstance(block, ResourceContentBlock):
            if _is_file_uri(block.uri):
                paths = _uri_paths(block.uri)
                if paths is None:
                    _add_context(f"[已忽略受限引用 {block.name or 'file'}：不合法的 file URI]")
                    continue
                lexical, resolved = paths
                if reason := session._refused_resource_reason(lexical, resolved):
                    _add_context(f"[已忽略受限引用 {block.name or resolved.name}：{reason}]")
                    continue
                if inside(session.cwd, lexical) and inside(session.cwd, resolved):
                    rel = resolved.relative_to(session.cwd).as_posix()
                    _add_context(f"@{quoted(rel)}")
                else:
                    _add_context(f"[外部文件引用：{resolved}，如需查看请调用 read_file]")
            else:
                _add_context(f"[{block.name or block.uri}]({block.uri})")
        elif isinstance(block, EmbeddedResourceContentBlock):
            res = block.resource
            if isinstance(res, TextResourceContents):
                if _is_file_uri(res.uri):
                    paths = _uri_paths(res.uri)
                    if paths is None:
                        _add_context("[已忽略受保护或被拒绝的上下文：不合法的 file URI]")
                        continue
                    lexical, resolved = paths
                    label = display_path(session.cwd, resolved)
                    if reason := session._refused_resource_reason(lexical, resolved):
                        _add_context(f"[已忽略受保护或被拒绝的上下文 {label}：{reason}]")
                        continue
                else:
                    label = res.uri
                _add_context(f'\n<context path="{label}">\n{res.text}\n</context>\n')
            elif isinstance(res, BlobResourceContents):
                if _is_file_uri(res.uri):
                    paths = _uri_paths(res.uri)
                    if paths is None:
                        _add_context("[已忽略受保护或被拒绝的资源：不合法的 file URI]")
                        continue
                    lexical, resolved = paths
                    label = display_path(session.cwd, resolved)
                    if reason := session._refused_resource_reason(lexical, resolved):
                        _add_context(f"[已忽略受保护或被拒绝的资源 {label}：{reason}]")
                        continue
                mime = res.mime_type or ""
                if mime.startswith("image/"):
                    decoded = _decode_image_block(res.blob, mime)
                    if decoded is not None:
                        img_counter += 1
                        mark = image_mark(img_counter)
                        images[mark] = decoded
                        _add_context(mark)
                    else:
                        _add_context(f"[无法解析的图片资源: {res.uri}]")
                else:
                    _add_context(f"[二进制资源: {res.uri}]")
        elif isinstance(block, ImageContentBlock):
            if block.uri and _is_file_uri(block.uri):
                paths = _uri_paths(block.uri)
                if paths is None:
                    _add_context("[已忽略受保护或被拒绝的图片：不合法的 file URI]")
                    continue
                lexical, resolved = paths
                label = display_path(session.cwd, resolved)
                if reason := session._refused_resource_reason(lexical, resolved):
                    _add_context(f"[已忽略受保护或被拒绝的图片 {label}：{reason}]")
                    continue
            decoded = _decode_image_block(block.data, block.mime_type)
            if decoded is not None:
                img_counter += 1
                mark = image_mark(img_counter)
                images[mark] = decoded
                _add_context(mark)
            else:
                _add_context("[无法解析的图片内容块]")
        elif isinstance(block, AudioContentBlock):
            _add_context("[当前模型会话暂不支持音频内容块]")

    user_text = "\n".join(part for part in user_parts if part)
    combined = "\n".join(part for part in ordered_parts if part).strip()
    if not combined and images:
        combined = " ".join(images.keys())
    return user_text, context_parts, combined, images
