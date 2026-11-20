"""User-facing MCP surface: prompt slash commands, @server:uri mentions, and /mcp inspection."""

from __future__ import annotations

import inspect
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .commands import NAME as COMMAND_NAME
from .commands import Command, CommandConflict, CommandContext, CommandRegistry
from .credentials import redact
from .images import ImageRef, with_alt
from .mcp_config import prompt_command_name, tool_name
from .messages import IMAGE_MARK, ProviderError, image_mark
from .permissions import Decision, Operation, normalize_mcp_uri
from .tools.base import ToolFailure

_IDENT_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
_ATTACHMENT_TAG = re.compile(r"<(\s*/?\s*(?:mcp-resource|file|directory)\b)", re.IGNORECASE)
_RESOURCE_TEXT_LIMIT = 65536
_OVERVIEW_ITEM_LIMIT = 8


class PromptArgumentError(ValueError):
    """Raised when user-supplied slash command arguments do not satisfy an MCP prompt spec."""


def _prompt_command_name(server: str, prompt: str) -> str:
    return prompt_command_name(server, prompt)


def prompt_argument_hint(args_spec: Sequence[Any]) -> str:
    return " ".join(
        f"<{arg.name}>" if getattr(arg, "required", False) else f"[{arg.name}]"
        for arg in args_spec
        if getattr(arg, "name", "")
    )


def _tokenize_prompt_args(raw: str) -> list[tuple[str, str | None]]:
    """Split raw CLI-style prompt input into `(value, key_or_none)` pairs while preserving Windows backslashes."""
    tokens: list[tuple[str, str | None]] = []
    n = len(raw)
    i = 0
    while i < n:
        while i < n and raw[i].isspace():
            i += 1
        if i >= n:
            break
        buf: list[str] = []
        quote: str | None = None
        quoted_before_eq = False
        eq_pos: int | None = None
        while i < n:
            ch = raw[i]
            if quote is None:
                if ch.isspace():
                    break
                if ch in ('"', "'"):
                    if eq_pos is None:
                        quoted_before_eq = True
                    quote = ch
                    i += 1
                    continue
                if ch == "=" and eq_pos is None and not quoted_before_eq:
                    eq_pos = len(buf)
                buf.append(ch)
                i += 1
            elif quote == "'":
                if ch == "'":
                    quote = None
                else:
                    buf.append(ch)
                i += 1
            else:
                if ch == '"':
                    quote = None
                    i += 1
                elif ch == "\\" and i + 1 < n and raw[i + 1] in ('"', "\\"):
                    buf.append(raw[i + 1])
                    i += 2
                else:
                    buf.append(ch)
                    i += 1
        if quote is not None:
            raise PromptArgumentError("参数引号未闭合。")
        full = "".join(buf)
        if eq_pos is not None and eq_pos > 0:
            candidate_key = full[:eq_pos]
            if _IDENT_KEY.fullmatch(candidate_key):
                tokens.append((full[eq_pos + 1 :], candidate_key))
                continue
        tokens.append((full, None))
    return tokens


def parse_prompt_arguments(raw: str, args_spec: Sequence[Any]) -> dict[str, str]:
    """Parse `key=value` and positional slash-command arguments against `MCPPromptArg` specs."""
    spec = tuple(args_spec or ())
    required = [arg.name for arg in spec if getattr(arg, "required", False)]
    stripped = raw.strip()
    if not stripped:
        if required:
            raise PromptArgumentError(f"缺少必填参数：{'、'.join(required)}。")
        return {}
    if not spec:
        raise PromptArgumentError("该提示词不接受参数。")

    try:
        tokens = _tokenize_prompt_args(stripped)
    except PromptArgumentError:
        # Unquoted natural prose (e.g. apostrophes) passed to a single-required-arg prompt stays intact.
        if (len(spec) == 1 or len(required) == 1) and not re.match(r"^[A-Za-z_][A-Za-z0-9_-]*=", stripped):
            target = spec[0].name if len(spec) == 1 else required[0]
            return {target: stripped}
        raise

    has_kv = any(key is not None for _, key in tokens)
    if not has_kv:
        if len(spec) == 1:
            return {spec[0].name: tokens[0][0] if len(tokens) == 1 else stripped}
        if len(required) == 1 and len(tokens) > len(spec):
            return {required[0]: stripped}
        if len(tokens) > len(spec):
            names = "、".join(arg.name for arg in spec)
            raise PromptArgumentError(f"位置参数过多：最多接受 {len(spec)} 个参数（{names}）。")
        parsed = {spec[idx].name: val for idx, (val, _) in enumerate(tokens)}
    else:
        known = {arg.name: arg for arg in spec}
        parsed = {}
        for val, key in tokens:
            if key is not None:
                if key not in known:
                    if tokens[0][1] is None and (len(spec) == 1 or (len(required) == 1 and len(tokens) > len(spec))):
                        target = spec[0].name if len(spec) == 1 else required[0]
                        return {target: stripped}
                    raise PromptArgumentError(f"未知参数：{key}。可用参数：{'、'.join(known)}。")
                if key in parsed:
                    raise PromptArgumentError(f"参数重复：{key}。")
                parsed[key] = val
            else:
                next_arg = next((arg.name for arg in spec if arg.name not in parsed), None)
                if next_arg is None:
                    names = "、".join(arg.name for arg in spec)
                    raise PromptArgumentError(f"位置参数过多：最多接受 {len(spec)} 个参数（{names}）。")
                parsed[next_arg] = val

    missing = [name for name in required if not parsed.get(name, "")]
    if missing:
        raise PromptArgumentError(f"缺少必填参数：{'、'.join(missing)}。")
    return parsed


def mcp_prompt_refusal(
    permissions: Any,
    server: str,
    prompt_name: str,
    command_name: str,
    arguments: Mapping[str, str],
) -> Decision | None:
    """Check explicit deny rules on the MCP server or prompt name before fetching the prompt."""
    if permissions is None:
        return None
    srv = server.strip()
    pname = prompt_name.strip()
    target = json.dumps(dict(arguments), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    candidates = dict.fromkeys((f"mcp__{srv}__{pname}", tool_name(srv, pname), command_name))
    for op_tool in candidates:
        if not op_tool:
            continue
        decision = permissions.decide(Operation(op_tool, target, server=srv), read_only=True)
        if decision.action == "deny":
            return decision
    return None


def _attach_prompt_images(prompt_text: str, image_refs: Sequence[Any]) -> tuple[str, dict[str, ImageRef]]:
    """Ensure every returned ImageRef has a unique `[图片 #N]` placeholder in `prompt_text`."""
    refs = [ref for ref in (image_refs or ()) if isinstance(ref, ImageRef)]
    if not refs:
        return prompt_text, {}
    existing = {int(m.group(1)) for m in IMAGE_MARK.finditer(prompt_text)}
    next_num = max(existing, default=0) + 1
    images: dict[str, ImageRef] = {}
    appended_marks: list[str] = []
    for ref in refs:
        mark = image_mark(next_num)
        next_num += 1
        images[mark] = with_alt(ref, mark)
        appended_marks.append(mark)
    marks_line = " ".join(appended_marks)
    combined = f"{prompt_text}\n\n{marks_line}" if prompt_text.strip() else marks_line
    return combined, images


def _accepts_images(conversation: Any) -> bool:
    provider_cfg = getattr(getattr(conversation, "provider", None), "config", None)
    return bool(getattr(provider_cfg, "accepts_images", True))


def _image_store(conversation: Any) -> Any:
    registry = getattr(conversation, "registry", None)
    if registry is not None and hasattr(registry, "store_image"):
        return registry
    session = getattr(conversation, "session", None)
    return session if session is not None and hasattr(session, "store_image") else None


def is_mcp_prompt_command(command: Command | None) -> bool:
    return bool(command is not None and command.kind == "prompt" and command.handler is not None)


def _typed_command(command: Command, argument: str) -> str:
    return f"/{command.name}" + (f" {argument}" if argument else "")


async def expand_mcp_prompt(
    conversation: Any,
    command: Command,
    argument: str,
    *,
    info: Any = None,
    manager: Any = None,
) -> tuple[str, str, dict[str, ImageRef]]:
    """Validate arguments and permissions, fetch the remote MCP prompt, and return `(text, label, images)`."""
    resolved_info = info
    if resolved_info is None:
        resolved_info = getattr(command.handler, "_mcp_prompt_info", None)
    resolved_manager = manager if manager is not None else getattr(conversation, "mcp_manager", None)
    if resolved_Manager_missing := (resolved_manager is None or resolved_info is None):
        if resolved_Manager_missing:
            raise ProviderError(f"MCP 提示词 /{command.name} 当前不可用。")

    args_spec = tuple(getattr(resolved_info, "arguments", ()) or ())
    try:
        parsed_args = parse_prompt_arguments(argument, args_spec)
    except PromptArgumentError as exc:
        raise PromptArgumentError(f"{exc} 用法：{command.usage}") from None

    registry = getattr(conversation, "registry", None)
    permissions = getattr(registry, "permissions", None) if registry is not None else None
    refused = mcp_prompt_refusal(
        permissions,
        resolved_info.server,
        resolved_info.name,
        command.name,
        parsed_args,
    )
    if refused is not None:
        raise ProviderError(f"MCP 提示词 /{command.name} 未执行：{refused.reason}")

    get_prompt = getattr(resolved_manager, "get_prompt", None)
    if not callable(get_prompt):
        raise ProviderError(f"MCP Server {resolved_info.server} 不支持获取提示词。")

    try:
        outcome = await get_prompt(
            resolved_info.server,
            resolved_info.name,
            parsed_args or None,
            accepts_images=_accepts_images(conversation),
            image_store=_image_store(conversation),
        )
    except ToolFailure as exc:
        raise ProviderError(redact(str(exc))) from None

    if isinstance(outcome, tuple) and len(outcome) >= 2:
        prompt_text, image_refs = str(outcome[0] or ""), tuple(outcome[1] or ())
    else:
        prompt_text, image_refs = str(outcome or ""), ()

    final_text, images = _attach_prompt_images(redact(prompt_text), image_refs)
    if not final_text.strip():
        raise ProviderError(f"MCP 提示词 /{command.name} 未返回内容。")
    return final_text, redact(_typed_command(command, argument)), images


async def _submit_ui_prompt(ui: Any, prompt: str, command_label: str, images: dict[str, ImageRef]) -> None:
    if images:
        sig = inspect.signature(ui.submit)
        accepts_kw = any(
            param.kind == inspect.Parameter.VAR_KEYWORD or name == "images" for name, param in sig.parameters.items()
        )
        if accepts_kw:
            await ui.submit(prompt, command=command_label, images=images)
            return
    await ui.submit(prompt, command=command_label)


def build_mcp_prompt_command(info: Any, manager: Any) -> Command:
    """Build a slash `Command` wrapping one discovered `MCPPromptInfo`."""
    cmd_name = getattr(info, "command_name", "") or _prompt_command_name(info.server, info.name)
    args_spec = tuple(getattr(info, "arguments", ()) or ())
    hint = prompt_argument_hint(args_spec)
    usage = f"/{cmd_name}" + (f" {hint}" if hint else "")
    if not args_spec:
        arg_mode = "none"
    elif any(getattr(arg, "required", False) for arg in args_spec):
        arg_mode = "required"
    else:
        arg_mode = "optional"

    raw_desc = (getattr(info, "description", "") or "").strip()
    summary = raw_desc.splitlines()[0].strip() if raw_desc else f"调用 MCP {info.server} 的提示词 {info.name}"
    description = f"{summary} [mcp:{info.server}]"

    detail_lines = [f"MCP 提示词：{info.server} / {info.name}"]
    if args_spec:
        detail_lines.append("参数：")
        for arg in args_spec:
            req = "必填" if getattr(arg, "required", False) else "可选"
            arg_desc = (getattr(arg, "description", "") or "").strip()
            detail_lines.append(f"  {arg.name}（{req}）" + (f"：{arg_desc}" if arg_desc else ""))

    async def _handler(ctx: CommandContext, argument: str) -> None:
        active_manager = getattr(ctx.conversation, "mcp_manager", None) or manager
        try:
            expanded, label, images = await expand_mcp_prompt(
                ctx.conversation,
                command,
                argument,
                info=info,
                manager=active_manager,
            )
        except PromptArgumentError as exc:
            await ctx.ui.note(str(exc))
            return
        except ProviderError as exc:
            await ctx.ui.note(f"! {exc}")
            return
        await _submit_ui_prompt(ctx.ui, expanded, label, images)

    _handler._mcp_prompt_info = info  # type: ignore[attr-defined]
    command = Command(
        name=cmd_name,
        description=description,
        usage=usage,
        kind="prompt",
        argument=arg_mode,
        argument_hint=hint,
        handler=_handler,
        template=summary,
        details="\n".join(detail_lines),
    )
    return command


def _iter_prompt_infos(status_or_prompts: Any) -> tuple[Any, ...]:
    if status_or_prompts is None:
        return ()
    if hasattr(status_or_prompts, "prompts"):
        if getattr(status_or_prompts, "state", "ready") != "ready":
            return ()
        return tuple(getattr(status_or_prompts, "prompts", ()) or ())
    if isinstance(status_or_prompts, Iterable):
        return tuple(status_or_prompts)
    return ()


def register_mcp_prompts(
    commands: CommandRegistry,
    status_or_prompts: Any,
    manager: Any,
    *,
    skipped: list[str] | None = None,
) -> list[str]:
    """Register discovered MCP prompts as `/mcp__...` commands; returns newly registered command names."""
    added: list[str] = []
    seen_conflicts: set[tuple[str, str]] | None = getattr(commands, "_mcp_seen_conflicts", None)
    if seen_conflicts is None:
        seen_conflicts = set()
        try:
            object.__setattr__(commands, "_mcp_seen_conflicts", seen_conflicts)
        except Exception:
            pass
    for info in _iter_prompt_infos(status_or_prompts):
        cmd_name = getattr(info, "command_name", "") or _prompt_command_name(info.server, info.name)
        if not COMMAND_NAME.fullmatch(cmd_name):
            if skipped is not None:
                skipped.append(f"MCP 提示词 {info.server}/{info.name} 未登记为命令：命令名 /{cmd_name} 不合法。")
            continue
        existing = commands.get(cmd_name)
        if existing is not None:
            prev = getattr(existing.handler, "_mcp_prompt_info", None)
            if prev is not None and (prev.server, prev.name) == (info.server, info.name):
                continue
            conflict_key = (info.server, info.name)
            if skipped is not None and conflict_key not in seen_conflicts:
                seen_conflicts.add(conflict_key)
                skipped.append(
                    f"MCP 提示词 {info.server}/{info.name} 未登记为命令：/{cmd_name} 已被 /{existing.name} 占用。"
                )
            continue
        command = build_mcp_prompt_command(info, manager)
        try:
            commands.register(command)
        except CommandConflict as exc:
            if skipped is not None:
                skipped.append(f"MCP 提示词 {info.server}/{info.name} 未登记为命令：{exc}")
            continue
        added.append(cmd_name)
    return added


def register_mcp_prompts_from_manager(
    commands: CommandRegistry,
    manager: Any,
    *,
    skipped: list[str] | None = None,
) -> list[str]:
    """Register prompts from all currently ready MCP servers in `manager`."""
    if manager is None:
        return []
    list_prompts = getattr(manager, "list_prompts", None)
    if callable(list_prompts):
        return register_mcp_prompts(commands, list_prompts(), manager, skipped=skipped)
    added: list[str] = []
    for status in getattr(manager, "statuses", lambda: ())():
        if getattr(status, "state", "") == "ready":
            added.extend(register_mcp_prompts(commands, status, manager, skipped=skipped))
    return added


def register_mcp_resource_tools(registry: Any, mcp_manager: Any) -> None:
    """Register `list_mcp_resources` and `read_mcp_resource` when MCP connections exist and tools are available."""
    if registry is None or mcp_manager is None:
        return
    has_servers = bool(
        getattr(mcp_manager, "connections", None)
        or getattr(mcp_manager, "statuses", lambda: ())()
        or callable(getattr(mcp_manager, "read_resource", None))
    )
    if not has_servers:
        return
    try:
        from .tools import mcp_resources
    except ImportError:
        return

    root = registry.permissions.root
    list_cls = getattr(mcp_resources, "ListMCPResourcesTool", None)
    read_cls = getattr(mcp_resources, "ReadMCPResourceTool", None)
    if list_cls is not None and registry.get("list_mcp_resources") is None:
        try:
            list_tool = list_cls(root, mcp_manager, permissions=registry.permissions)
        except TypeError:
            list_tool = list_cls(root, mcp_manager)
        registry.register(list_tool)
    if read_cls is not None and registry.get("read_mcp_resource") is None:
        registry.register(read_cls(root, mcp_manager))


# @server:uri mentions and autocomplete ---------------------------------------------------


def known_mcp_servers(manager: Any) -> set[str]:
    """Server names currently ready (or configured on lightweight fakes) in `manager`."""
    if manager is None:
        return set()
    names: set[str] = set()
    statuses_fn = getattr(manager, "statuses", None)
    if callable(statuses_fn):
        statuses = tuple(statuses_fn() or ())
        names.update(s.name for s in statuses if getattr(s, "state", "") == "ready")
        if statuses:
            return names
    for conn in getattr(manager, "connections", ()) or ():
        cfg = getattr(conn, "config", None)
        if cfg is not None and getattr(conn, "state", "ready") == "ready":
            names.add(cfg.name)
    for getter in ("list_resources", "list_resource_templates", "list_prompts"):
        fn = getattr(manager, getter, None)
        if callable(fn):
            for item in fn() or ():
                if srv := getattr(item, "server", ""):
                    names.add(srv)
    for srv in getattr(manager, "servers", ()) or ():
        names.add(srv if isinstance(srv, str) else getattr(srv, "name", ""))
    names.discard("")
    return names


def escape_xml_attr(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\r", "&#13;")
        .replace("\n", "&#10;")
    )


def unescape_xml_attr(value: str) -> str:
    return (
        value.replace("&#10;", "\n")
        .replace("&#13;", "\r")
        .replace("&gt;", ">")
        .replace("&lt;", "<")
        .replace("&quot;", '"')
        .replace("&amp;", "&")
    )


def escape_mcp_resource_xml(text: str) -> str:
    """Neutralize `<mcp-resource>`, `<file>`, and `<directory>` tags inside untrusted MCP resource payloads."""
    return _ATTACHMENT_TAG.sub(r"&lt;\1", text)


def _cap_resource_text(text: str) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= _RESOURCE_TEXT_LIMIT:
        return text
    clipped = encoded[:_RESOURCE_TEXT_LIMIT].decode("utf-8", errors="ignore")
    return clipped + "\n…（MCP 资源内容超过 64 KB，已截断）"


def mcp_resource_refusal(registry: Any, server: str, uri: str) -> Decision | None:
    """Enforce PermissionPolicy (`typed=True`) for user-typed `@server:uri` resource mentions."""
    if registry is None:
        return Decision("deny", "unknown_tool", "当前没有可用的权限注册表。")
    srv = server.strip()
    raw_uri = uri.strip()
    canon_uri = normalize_mcp_uri(raw_uri)
    read_tool = registry.get("read_mcp_resource")
    if read_tool is not None:
        try:
            params = read_tool.validate_arguments({"server": srv, "uri": raw_uri})
            refused = registry.refusal(read_tool, params)
            if refused is not None:
                return refused
        except Exception:
            pass
    permissions = getattr(registry, "permissions", None)
    if permissions is None:
        return None
    target = f"{srv}:{canon_uri}"
    for op in (
        Operation("read_mcp_resource", target, server=srv),
        Operation("read_mcp_resource", f"{srv}:{raw_uri}", server=srv),
        Operation(f"mcp__{srv}__read_resource", canon_uri, server=srv),
    ):
        decision = permissions.decide(op, read_only=True)
        if decision.action == "deny":
            return decision
    return None


async def read_mcp_resource_for_mention(
    registry: Any,
    manager: Any,
    server: str,
    uri: str,
) -> tuple[str, tuple[ImageRef, ...], str | None]:
    """Read one MCP resource for `@server:uri`; returns `(safe_xml_body, images, error_reason_or_none)`."""
    srv = server.strip()
    clean_uri = uri.strip()
    refused = mcp_resource_refusal(registry, srv, clean_uri)
    if refused is not None:
        return f"未读取：{refused.reason}", (), refused.reason
    if manager is None or not callable(getattr(manager, "read_resource", None)):
        reason = f"MCP Server {srv} 当前不可用。"
        return f"未读取：{reason}", (), reason
    accepts = bool(getattr(registry, "images", True))
    store = registry if hasattr(registry, "store_image") else None
    try:
        outcome = await manager.read_resource(srv, clean_uri, accepts_images=accepts, image_store=store)
    except ToolFailure as exc:
        msg = redact(str(exc))
        return f"错误 [{exc.code}]：{msg}", (), msg
    except ProviderError as exc:
        msg = redact(str(exc))
        return f"未读取：{msg}", (), msg
    except Exception:
        reason = "读取 MCP 资源失败。"
        return f"未读取：{reason}", (), reason

    if isinstance(outcome, tuple) and len(outcome) >= 2:
        raw_text = str(outcome[0] or "")
        images = tuple(img for img in (outcome[1] or ()) if isinstance(img, ImageRef))
    else:
        raw_text, images = str(outcome or ""), ()
    capped = _cap_resource_text(redact(raw_text))
    safe_body = escape_mcp_resource_xml(capped)
    return safe_body, images, None


def _server_denied(permissions: Any, server: str) -> bool:
    if permissions is None:
        return False
    srv = server.strip()
    decision = permissions.decide(Operation("read_mcp_resource", srv, server=srv), read_only=True)
    return decision.action == "deny"


def _uri_denied(permissions: Any, server: str, uri: str) -> bool:
    if permissions is None:
        return False
    srv = server.strip()
    raw_uri = uri.strip()
    decision = permissions.decide(Operation("read_mcp_resource", f"{srv}:{raw_uri}", server=srv), read_only=True)
    return decision.action == "deny"


def mcp_mention_candidates(
    manager: Any,
    query: str,
    *,
    permissions: Any = None,
    limit: int = 12,
) -> list[tuple[str, str, bool]]:
    """Return `(token, description, keep_open)` completion candidates for `@server:` and `@server:uri`."""
    if manager is None or limit <= 0:
        return []
    servers = sorted(s for s in known_mcp_servers(manager) if not _server_denied(permissions, s))
    if not servers:
        return []

    list_res = getattr(manager, "list_resources", None)
    list_tpl = getattr(manager, "list_resource_templates", None)
    resources_by_server: dict[str, list[Any]] = {s: [] for s in servers}
    templates_by_server: dict[str, list[Any]] = {s: [] for s in servers}

    if callable(list_res):
        for item in list_res() or ():
            if item.server in resources_by_server and not _uri_denied(permissions, item.server, item.uri):
                resources_by_server[item.server].append(item)
    else:
        for status in getattr(manager, "statuses", lambda: ())() or ():
            if getattr(status, "state", "") == "ready" and status.name in resources_by_server:
                for item in getattr(status, "resources", ()) or ():
                    if not _uri_denied(permissions, status.name, getattr(item, "uri", "")):
                        resources_by_server[status.name].append(item)

    if callable(list_tpl):
        for item in list_tpl() or ():
            if item.server in templates_by_server and not _uri_denied(permissions, item.server, item.uri_template):
                templates_by_server[item.server].append(item)
    else:
        for status in getattr(manager, "statuses", lambda: ())() or ():
            if getattr(status, "state", "") == "ready" and status.name in templates_by_server:
                for item in getattr(status, "resource_templates", ()) or ():
                    if not _uri_denied(permissions, status.name, getattr(item, "uri_template", "")):
                        templates_by_server[status.name].append(item)

    # Exact concrete resource already completed -> let Enter submit.
    if ":" in query and not query.endswith(":"):
        prefix, _, rest = query.partition(":")
        if prefix in resources_by_server and any(getattr(r, "uri", "") == rest for r in resources_by_server[prefix]):
            return []

    if not query:
        out: list[tuple[str, str, bool]] = []
        for srv in servers:
            total = len(resources_by_server[srv]) + len(templates_by_server[srv])
            if total > 0:
                out.append((f"{srv}:", f"MCP 资源（{total} 项）", True))
        return out[:limit]

    if ":" in query:
        prefix, _, rest = query.partition(":")
        if prefix not in resources_by_server:
            return []
        needle = rest.casefold()
        entries: list[tuple[str, str, bool]] = []
        for res in resources_by_server[prefix]:
            uri = getattr(res, "uri", "")
            detail = (getattr(res, "description", "") or getattr(res, "title", "") or getattr(res, "name", "")).strip()
            label_desc = f"MCP 资源 · {detail}" if detail else "MCP 资源"
            if not needle or needle in uri.casefold() or needle in detail.casefold():
                entries.append((f"{prefix}:{uri}", label_desc, False))
        for tpl in templates_by_server[prefix]:
            uri_tpl = getattr(tpl, "uri_template", "")
            detail = (getattr(tpl, "description", "") or getattr(tpl, "title", "") or getattr(tpl, "name", "")).strip()
            label_desc = f"MCP 资源模板 · {detail}" if detail else "MCP 资源模板"
            if not needle or needle in uri_tpl.casefold() or needle in detail.casefold():
                entries.append((f"{prefix}:{uri_tpl}", label_desc, "{" in uri_tpl))
        return entries[:limit]

    from textual.fuzzy import Matcher

    matcher = Matcher(query)
    scored: list[tuple[float, str, str, bool]] = []
    for srv in servers:
        total = len(resources_by_server[srv]) + len(templates_by_server[srv])
        if total > 0 and (score := matcher.match(f"{srv}:")) > 0:
            scored.append((score, f"{srv}:", f"MCP 资源（{total} 项）", True))
        for res in resources_by_server[srv]:
            token = f"{srv}:{res.uri}"
            detail = (getattr(res, "description", "") or getattr(res, "title", "") or getattr(res, "name", "")).strip()
            if (score := matcher.match(token)) > 0:
                scored.append((score, token, f"MCP 资源 · {detail}" if detail else "MCP 资源", False))
        for tpl in templates_by_server[srv]:
            token = f"{srv}:{tpl.uri_template}"
            detail = (getattr(tpl, "description", "") or getattr(tpl, "title", "") or getattr(tpl, "name", "")).strip()
            if (score := matcher.match(token)) > 0:
                scored.append(
                    (score, token, f"MCP 资源模板 · {detail}" if detail else "MCP 资源模板", "{" in tpl.uri_template)
                )
    scored.sort(key=lambda item: (-item[0], len(item[1]), item[1]))
    return [(tok, desc, keep_open) for _, tok, desc, keep_open in scored[:limit]]


# /mcp inspection formatting --------------------------------------------------------------


def describe_mcp_status(status: Any) -> str:
    """Format `status.describe()` with resource/prompt counts if the status object did not include them."""
    base = status.describe()
    if getattr(status, "state", "") != "ready":
        return base
    resources = getattr(status, "resources", ()) or ()
    templates = getattr(status, "resource_templates", ()) or ()
    prompts = getattr(status, "prompts", ()) or ()
    total_res = len(resources) + len(templates)
    if total_res and "资源" not in base:
        base += f" · {total_res} 个资源"
    if prompts and "提示词" not in base:
        base += f" · {len(prompts)} 个提示词"
    return base


def _one_line(text: str, limit: int = 80) -> str:
    cleaned = " ".join((text or "").split()).strip()
    return cleaned[:limit]


def format_mcp_capabilities(status: Any, *, cap: int | None = None) -> list[str]:
    """Format resources, resource templates, and prompt slash commands for a ready `ServerStatus`."""
    if getattr(status, "state", "") not in {"ready", "closed"}:
        return []
    resources = tuple(getattr(status, "resources", ()) or ())
    templates = tuple(getattr(status, "resource_templates", ()) or ())
    prompts = tuple(getattr(status, "prompts", ()) or ())
    lines: list[str] = []

    shown_res = resources[:cap] if cap is not None else resources
    for res in shown_res:
        desc = _one_line(getattr(res, "description", "") or getattr(res, "title", ""))
        mime = getattr(res, "mime_type", "") or ""
        meta = " · ".join(part for part in (mime, desc) if part)
        lines.append(f"  资源 @{status.name}:{res.uri}" + (f"  {meta}" if meta else ""))
    if cap is not None and len(resources) > cap:
        lines.append(f"  …（另有 {len(resources) - cap} 个资源，输入 /mcp {status.name} 查看全部）")

    shown_tpl = templates[:cap] if cap is not None else templates
    for tpl in shown_tpl:
        desc = _one_line(getattr(tpl, "description", "") or getattr(tpl, "title", ""))
        mime = getattr(tpl, "mime_type", "") or ""
        meta = " · ".join(part for part in (mime, desc) if part)
        lines.append(f"  资源模板 {tpl.uri_template}" + (f"  {meta}" if meta else ""))
    if cap is not None and len(templates) > cap:
        lines.append(f"  …（另有 {len(templates) - cap} 个资源模板，输入 /mcp {status.name} 查看全部）")

    shown_prompts = prompts[:cap] if cap is not None else prompts
    for prompt in shown_prompts:
        cmd_name = getattr(prompt, "command_name", "") or _prompt_command_name(status.name, prompt.name)
        hint = prompt_argument_hint(getattr(prompt, "arguments", ()) or ())
        usage = f"/{cmd_name}" + (f" {hint}" if hint else "")
        desc = _one_line(getattr(prompt, "description", ""))
        lines.append(f"  提示词 {usage}" + (f"  {desc}" if desc else ""))
    if cap is not None and len(prompts) > cap:
        lines.append(f"  …（另有 {len(prompts) - cap} 个提示词，输入 /mcp {status.name} 查看全部）")

    return lines


def overview_capability_lines(status: Any) -> list[str]:
    if getattr(status, "state", "") != "ready":
        return []
    return format_mcp_capabilities(status, cap=_OVERVIEW_ITEM_LIMIT)
