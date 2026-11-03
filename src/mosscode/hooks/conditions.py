"""`field op value` conditions, joined by && or ||, with the permission rules' matching."""

import fnmatch
import os
import re
from dataclasses import dataclass
from pathlib import Path

from ..permissions import ALIASES, normalized, path_matches
from ..security import command_segments, starts_with
from .events import ALL_FIELDS, EVENTS, fields_of

OPERATORS = ("==", "!=", "~=", "=~")
ARGS_NAME = re.compile(r"[A-Za-z0-9_-]+")
CONDITION = re.compile(r"\s*([A-Za-z_][A-Za-z0-9_.-]*)\s*(==|!=|~=|=~)\s*(.*?)\s*", re.DOTALL)


class HookConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Condition:
    field: str  # "tool", "path", "args.command" ...
    op: str  # "==" "!=" "~=" "=~"
    value: str  # tool aliases resolved, paths normalized
    regex: re.Pattern | None = None

    def matches(self, fields) -> bool:
        actual = str(fields.get(self.field, ""))
        if self.op == "=~":
            return bool(self.regex.search(actual))
        if self.op in {"==", "!="}:
            return _equal(self.field, actual, self.value) == (self.op == "==")
        return _glob(self.field, actual, self.value)


@dataclass(frozen=True)
class ConditionGroup:
    items: tuple[Condition, ...]
    any: bool = False  # True: ||; False: &&
    text: str = ""  # As written, for /hooks.

    def matches(self, fields) -> bool:
        results = (item.matches(fields) for item in self.items)
        return any(results) if self.any else all(results)


def _equal(field: str, actual: str, expected: str) -> bool:
    if field == "path":
        actual = normalized(actual) if actual else ""
        if os.name == "nt":
            return actual.casefold() == expected.casefold()
    return actual == expected


def _glob(field: str, actual: str, pattern: str) -> bool:
    if field == "path":
        # Relative patterns cover the project only; absolute ones cover outside targets (as Rule.matches).
        if not actual or Path(pattern).is_absolute() != Path(actual).is_absolute():
            return False
        return path_matches(actual, pattern)
    if field == "command":
        if not actual:
            return False
        candidates = [actual, *command_segments(actual)]
        if pattern.endswith(":*"):
            return any(starts_with(candidate, pattern[:-2]) for candidate in candidates)
        return any(fnmatch.fnmatchcase(candidate, pattern) for candidate in candidates)
    return fnmatch.fnmatchcase(actual, pattern)


def _split(expression: str) -> tuple[list[str], set[str]]:
    """Split on && and || outside double quotes."""
    parts, joins, current, quoted, index = [], set(), [], False, 0
    while index < len(expression):
        char = expression[index]
        if quoted:
            current.append(char)
            if char == "\\" and index + 1 < len(expression):
                current.append(expression[index + 1])
                index += 2
                continue
            if char == '"':
                quoted = False
        elif char == '"':
            quoted = True
            current.append(char)
        elif expression.startswith(("&&", "||"), index):
            parts.append("".join(current))
            joins.add(expression[index : index + 2])
            current = []
            index += 2
            continue
        else:
            current.append(char)
        index += 1
    if quoted:
        raise HookConfigError("条件中的双引号没有闭合。")
    parts.append("".join(current))
    return parts, joins


def _unquote(value: str) -> str:
    if not value.startswith('"'):
        return value
    if len(value) < 2 or not value.endswith('"'):
        raise HookConfigError(f"条件值的引号不完整：{value}")
    inner, result, index = value[1:-1], [], 0
    while index < len(inner):
        char = inner[index]
        if char == "\\" and index + 1 < len(inner) and inner[index + 1] in '"\\':
            result.append(inner[index + 1])
            index += 2
            continue
        if char == '"':
            raise HookConfigError(f'条件值中的引号需写成 \\"：{value}')
        result.append(char)
        index += 1
    return "".join(result)


def check_field(name: str, event: str) -> None:
    """A field this event has; args.<name> on tool events only, one level."""
    if name.startswith("args."):
        if not EVENTS[event].tool:
            raise HookConfigError(f"事件 {event} 没有条件字段 {name}（args.* 只用于工具事件）。")
        if not ARGS_NAME.fullmatch(name[len("args.") :]):
            raise HookConfigError(f"args 字段只支持顶层参数名（如 args.command）：{name}")
        return
    if name not in ALL_FIELDS:
        raise HookConfigError(f"未知条件字段：{name}")
    if name not in fields_of(event):
        raise HookConfigError(f"事件 {event} 没有条件字段 {name}。")


def _condition(text: str, event: str) -> Condition:
    if not text.strip():
        raise HookConfigError("条件中有空的子条件（检查 && 或 || 两侧）。")
    match = CONDITION.fullmatch(text)
    if not match:
        raise HookConfigError(f"子条件需为“字段 运算符 值”，运算符为 {' '.join(OPERATORS)}：{text.strip()}")
    name, op, raw = match[1], match[2], match[3]
    check_field(name, event)
    if not raw:
        raise HookConfigError(f'子条件缺少值（空值请写 ""）：{text.strip()}')
    value = _unquote(raw)
    if op == "=~":
        if len(value) < 2 or not value.startswith("/") or not value.endswith("/"):
            raise HookConfigError(f"正则需写成 /…/：{raw}")
        try:
            return Condition(name, op, value, re.compile(value[1:-1]))
        except re.error as exc:
            raise HookConfigError(f"正则不合法（{exc}）：{raw}") from None
    if name == "tool" and (op != "~=" or value in ALIASES):
        value = ALIASES.get(value, value)
    elif name == "path" and op != "~=":
        value = normalized(value)
    elif name == "path":
        value = value.replace("\\", "/")
    return Condition(name, op, value)


def parse_condition(expression, event: str) -> ConditionGroup:
    if not isinstance(expression, str) or not expression.strip():
        raise HookConfigError("if 需为非空的条件字符串。")
    parts, joins = _split(expression)
    if len(joins) > 1:
        raise HookConfigError("同一条件中不能混用 && 和 ||；需要时拆成多条 Hook。")
    return ConditionGroup(tuple(_condition(part, event) for part in parts), joins == {"||"}, expression.strip())
