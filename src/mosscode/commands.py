"""Slash commands: one registry for parsing, dispatch, completion and /help."""

from __future__ import annotations

import difflib
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, Protocol

if TYPE_CHECKING:
    from .config import Settings
    from .conversation import Conversation
    from .messages import Message
    from .skills import SkillCatalog

NAME = re.compile(r"[a-z][a-z0-9_-]*")
WORD = re.compile(r"/[A-Za-z][A-Za-z0-9_-]*")
_SPLIT = re.compile(r"(\S+)(?:\s+(.*))?", re.DOTALL)
SUGGEST_CUTOFF = 0.6


class CommandConflict(ValueError):
    """A built-in command is malformed or reuses a name; raised at import, so startup fails."""


class Hint(NamedTuple):
    value: str  # What Tab writes into the input.
    description: str
    label: str = ""  # Shown in the menu instead of value, e.g. with the argument hint.


class CommandUI(Protocol):
    """What a command may do to the interface. State lives on Conversation, not here."""

    provider_name: str
    model: str

    async def note(self, text: str, style: str | None = None) -> None: ...

    async def submit(self, prompt: str, *, command: str | None = None, synthetic: str | None = None) -> None: ...

    def refresh_status(self) -> None: ...

    async def clear_screen(self) -> None: ...

    def choose(
        self,
        title: str,
        options: Sequence[Any],
        current: int,
        on_pick: Callable[[int], Awaitable[None]],
        on_cancel: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """Open a picker and return at once; on_pick runs with the chosen index, Esc runs on_cancel (if any)."""

    async def compact(self, instructions: str) -> None: ...

    def resume(self, session_id: str) -> None: ...

    async def switch_model(self, name: str) -> None: ...

    async def rewound(self, message: Message) -> None:
        """The conversation went back to before message: show what is left and put it back in the input."""

    def exit(self) -> None: ...


@dataclass
class CommandContext:
    conversation: Conversation
    ui: CommandUI
    registry: CommandRegistry
    settings: Settings | None = None


Handler = Callable[[CommandContext, str], Awaitable[None]]
Completer = Callable[[CommandContext], Sequence[tuple[str, str]]]


@dataclass(frozen=True)
class Command:
    name: str  # Lower case, without the slash.
    description: str  # One line, for the menu and /help.
    usage: str  # "/compact [说明]"
    kind: Literal["local", "prompt", "skill"]
    aliases: tuple[str, ...] = ()
    argument: Literal["none", "optional", "required"] = "none"
    argument_hint: str = ""  # Shown after the name in the menu, e.g. "[说明]".
    hidden: bool = False
    handler: Handler | None = None  # local
    template: str = ""  # prompt
    complete: Completer | None = None  # Argument candidates as (value, description).
    skill: str = ""  # skill: the SkillDef it runs.
    # Safe during a run (reads state or stops a task); others wait. Or a test of the argument.
    while_running: bool | Callable[[str], bool] = False
    details: str = ""  # More lines for /help <command>, e.g. its subcommands.

    @property
    def names(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)

    def runs_while_running(self, argument: str) -> bool:
        return self.while_running(argument) if callable(self.while_running) else self.while_running

    def expand(self, argument: str) -> str:
        """The template, with the argument appended as an extra requirement."""
        return self.template + (f"\n\n补充要求：{argument}" if argument else "")


@dataclass(frozen=True)
class Parsed:
    kind: Literal["empty", "message", "command", "unknown", "shell"]
    text: str = ""  # message: what goes to the model; shell: the command
    command: Command | None = None
    argument: str = ""
    word: str = ""  # The command word as typed


class CommandRegistry:
    def __init__(self, commands: Sequence[Command] = ()):
        self._commands: list[Command] = []
        self._index: dict[str, Command] = {}
        for command in commands:
            self.register(command)

    def register(self, command: Command) -> None:
        for name in command.names:
            if not NAME.fullmatch(name):
                raise CommandConflict(
                    f"命令名 /{name} 不合法：须以小写字母开头，只含小写字母、数字、- 和 _（/{command.name}）。"
                )
        folded = [name.lower() for name in command.names]
        if len(set(folded)) != len(folded):
            raise CommandConflict(f"命令 /{command.name} 的名称和别名重复。")
        for name in folded:
            if name in self._index:
                raise CommandConflict(f"/{name} 冲突：命令 /{self._index[name].name} 和 /{command.name} 都使用了它。")
        if command.kind == "local" and command.handler is None:
            raise CommandConflict(f"本地命令 /{command.name} 缺少处理函数。")
        if command.kind == "prompt" and not command.template.strip():
            raise CommandConflict(f"提示词命令 /{command.name} 缺少模板。")
        if command.kind == "skill" and not command.skill:
            raise CommandConflict(f"Skill 命令 /{command.name} 缺少 Skill 名称。")
        self._commands.append(command)
        self._index.update((name, command) for name in folded)

    def names(self) -> tuple[str, ...]:
        """Every name and alias, for completion."""
        return tuple(self._index)

    def with_skills(self, catalog: SkillCatalog) -> tuple[CommandRegistry, tuple[str, ...]]:
        """A copy for one run with a command per skill; the built-in table is never changed."""
        registry = CommandRegistry(command for command in self._commands if command.kind != "skill")
        return registry, registry.set_skills(catalog)

    def set_skills(self, catalog: SkillCatalog) -> tuple[str, ...]:
        """Replace the skill commands; a skill whose name is taken is skipped with a problem."""
        self._commands = [command for command in self._commands if command.kind != "skill"]
        self._index = {name: command for name, command in self._index.items() if command.kind != "skill"}
        problems = []
        for skill in catalog.skills.values():
            hint = skill.argument_hint or "[参数]"
            command = Command(
                skill.name,
                f"{skill.description} [skill]",
                f"/{skill.name} {hint}",
                "skill",
                argument="optional",
                argument_hint=hint,
                hidden=not skill.user_invocable,
                skill=skill.name,
            )
            try:
                self.register(command)
            except CommandConflict as exc:
                problems.append(f"Skill {skill.name} 未登记为命令：{exc}")
        return tuple(problems)

    def get(self, word: str) -> Command | None:
        return self._index.get(word.removeprefix("/").lower())

    def visible(self) -> list[Command]:
        return [command for command in self._commands if not command.hidden]

    def suggest(self, word: str) -> str | None:
        names = [name for command in self.visible() for name in command.names]
        close = difflib.get_close_matches(word.removeprefix("/").lower(), names, n=1, cutoff=SUGGEST_CUTOFF)
        return f"/{self._index[close[0]].name}" if close else None

    def matches(self, ctx: CommandContext, value: str) -> list[Hint]:
        """Commands by prefix (aliases point at their command), then argument candidates."""
        if "\n" in value:
            return []
        value = value.lstrip()
        word, separator, argument = value.partition(" ")
        if separator:
            command = self.get(word) if WORD.fullmatch(word) else None
            if command is None or command.complete is None:
                return []
            prefix = argument.lstrip()
            return [
                Hint(f"/{command.name} {item}", description)
                for item, description in command.complete(ctx)
                if item.startswith(prefix)
            ]
        if not value.startswith("/"):
            return []
        prefix = value[1:].lower()
        hints = []
        for command in self.visible():
            label = f"/{command.name}" + (f" {command.argument_hint}" if command.argument_hint else "")
            if command.name.startswith(prefix):
                hints.append(Hint(f"/{command.name}", command.description, label))
            elif alias := next((a for a in command.aliases if a.startswith(prefix)), None):
                hints.append(Hint(f"/{command.name}", f"{command.description}（别名 /{alias}）", label))
        return hints

    def takes_completion(self, ctx: CommandContext, value: str) -> bool:
        """Tab after a full command name moves on to its arguments when there are candidates."""
        command = self.get(value.strip()) if WORD.fullmatch(value.strip()) else None
        return bool(command and command.complete and command.complete(ctx))


def parse_input(text: str, registry: CommandRegistry) -> Parsed:
    """Commands are one slash word, shell commands start with ! (!! keeps them out of the context);
    anything else (paths, "//" escapes) is a message."""
    if not text.strip():
        return Parsed("empty")
    stripped = text.lstrip()
    if stripped.startswith("!"):
        word = "!!" if stripped.startswith("!!") else "!"
        return Parsed("shell", stripped[len(word) :].strip(), word=word)
    if stripped.startswith("//"):
        return Parsed("message", stripped[1:])
    if stripped.rstrip() == "/":
        return Parsed("unknown", word="/")
    match = _SPLIT.fullmatch(stripped.rstrip())
    word, rest = match.group(1), match.group(2) or ""
    if not WORD.fullmatch(word):
        return Parsed("message", text)
    command = registry.get(word)
    if command is None:
        return Parsed("unknown", word=word)
    return Parsed("command", command=command, argument=rest.strip(), word=word)


def unknown_note(parsed: Parsed, registry: CommandRegistry) -> str:
    if parsed.word == "/":
        return "输入 /help 查看命令。"
    suggestion = registry.suggest(parsed.word)
    lead = f"是不是想输入 {suggestion}？" if suggestion else ""
    return f"{lead}未知命令 {parsed.word}。输入 /help 查看命令；要把以 / 开头的文字发给模型，在开头再加一个 /。"


def argument_problem(command: Command, argument: str) -> str | None:
    """None when the argument fits the command."""
    if command.argument == "none" and argument:
        return f"/{command.name} 不接受参数。用法：{command.usage}"
    if command.argument == "required" and not argument:
        return f"/{command.name} 需要参数。用法：{command.usage}"
    return None
