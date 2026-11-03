"""Standard-library fallback, isolated so regex/search can be timed out.

Output mirrors the ripgrep invocations in search.py: relative paths for glob,
`--json` match/context events for grep content, `path NUL count` for counts.
"""

import fnmatch
import json
import os
import re
import sys
from collections import deque
from pathlib import Path

SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__"}
FOLD = os.name == "nt"  # Git for Windows matches ignore patterns case-insensitively.


def expand_braces(pattern):
    """Expand `{a,b}` alternatives (nested too) like rg globs; unbalanced braces stay literal."""
    depth, start = 0, None
    for index, char in enumerate(pattern):
        if char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth:
            depth -= 1
            if depth:
                continue
            body, rest = pattern[start + 1 : index], pattern[index + 1 :]
            alternatives, level, last = [], 0, 0
            for position, inner in enumerate(body):
                if inner == "{":
                    level += 1
                elif inner == "}":
                    level -= 1
                elif inner == "," and level == 0:
                    alternatives.append(body[last:position])
                    last = position + 1
            if not alternatives:  # `{x}` has no alternatives: keep it, but expand inside and after it.
                return [
                    pattern[:start] + "{" + inner + "}" + tail
                    for inner in expand_braces(body)
                    for tail in expand_braces(rest)
                ]
            alternatives.append(body[last:])
            return list(
                dict.fromkeys(
                    expanded
                    for alternative in alternatives
                    for expanded in expand_braces(pattern[:start] + alternative + rest)
                )
            )
    if depth:  # The first '{' never closed: literal, but later braces may still pair up.
        return [pattern[: start + 1] + tail for tail in expand_braces(pattern[start + 1 :])]
    return [pattern]


def matches(path, pattern):
    # Glob '*' matches a single path segment; '**' spans zero or more.
    parts = path.replace("\\", "/").split("/")
    patterns = pattern.replace("\\", "/").split("/")

    def match(index, position):
        if position == len(patterns):
            return index == len(parts)
        if patterns[position] == "**":
            return match(index, position + 1) or (index < len(parts) and match(index + 1, position))
        return (
            index < len(parts)
            and fnmatch.fnmatchcase(parts[index], patterns[position])
            and match(index + 1, position + 1)
        )

    return match(0, 0)


class Ignore:
    """The subset of .gitignore semantics that decides most real files:
    comments, `!` negation, `/` anchoring, trailing-`/` directory patterns, `**`."""

    def __init__(self, rules=()):
        self.rules = tuple(rules)

    def extend(self, directory: Path) -> "Ignore":
        try:
            text = (directory / ".gitignore").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return self
        rules = list(self.rules)
        for line in text.splitlines():
            line = line.rstrip()
            if not line or line.startswith("#"):
                continue
            negate = line.startswith("!")
            if negate:
                line = line[1:]
            if line.startswith("\\"):
                line = line[1:]
            dir_only = line.endswith("/")
            line = line.rstrip("/")
            anchored = "/" in line
            line = line.lstrip("/")
            if line:
                rules.append((directory, line.casefold() if FOLD else line, negate, dir_only, anchored))
        return Ignore(rules)

    def ignored(self, path: Path, is_dir: bool) -> bool:
        result = False
        name = path.name.casefold() if FOLD else path.name
        for base, pattern, negate, dir_only, anchored in self.rules:
            if dir_only and not is_dir:
                continue
            if anchored:
                try:
                    relative = path.relative_to(base).as_posix()
                except ValueError:
                    continue
                hit = matches(relative.casefold() if FOLD else relative, pattern)
            else:
                hit = fnmatch.fnmatchcase(name, pattern)
            if hit:
                result = not negate  # Last matching rule wins.
        return result


def initial_ignore(root: Path, base: Path | None) -> Ignore:
    """Ignore files from the project root down to the search root (inclusive)."""
    ignore = Ignore()
    if base is None or not root.is_relative_to(base):
        return ignore.extend(root)
    parts = root.relative_to(base).parts
    for depth in range(len(parts) + 1):
        ignore = ignore.extend(base.joinpath(*parts[:depth]))
    return ignore


def files(root: Path, base: Path | None):
    if root.is_file():
        yield root
        return
    if not root.is_dir():
        raise FileNotFoundError()
    boundary = root.resolve()
    ignores = {root: initial_ignore(root, base)}
    for directory, folders, names in os.walk(root):
        current = Path(directory)
        ignore = ignores.pop(current, None) or Ignore()
        kept = []
        for name in folders:
            path = current / name
            if (
                name in SKIP_DIRS
                or (name == "worktrees" and current.name == ".mosscode")
                or path.is_symlink()
                or path.is_junction()
                or not path.resolve().is_relative_to(boundary)
                or ignore.ignored(path, True)
            ):
                continue
            kept.append(name)
            ignores[path] = ignore.extend(path)
        folders[:] = kept
        for name in names:
            path = current / name
            if ignore.ignored(path, False):
                continue
            try:
                if path.resolve().is_relative_to(boundary):
                    yield path
            except (OSError, RuntimeError):
                continue


def grep_file(path, expression, context):
    """Yield (kind, line_number, text) like rg --json match/context events."""
    before = deque(maxlen=context)
    after = 0
    with path.open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if "\0" in line:
                return
            if expression.search(line):
                yield from (("context", n, text) for n, text in before)
                before.clear()
                yield "match", number, line
                after = context
            elif after:
                yield "context", number, line
                after -= 1
            elif context:
                before.append((number, line))


def main():
    mode, arguments = sys.argv[1], json.loads(sys.argv[2])
    root = Path(arguments["path"])
    base = Path(arguments["base"]) if arguments.get("base") else None
    flags = re.IGNORECASE if arguments.get("ignore_case") else 0
    expression = re.compile(arguments["pattern"], flags) if mode != "glob" else None
    pattern = arguments["pattern"] if mode == "glob" else arguments.get("glob")
    # Braces are expanded for user patterns only; .gitignore has no brace syntax.
    patterns = expand_braces(pattern) if pattern else []
    for path in files(root, base):
        relative = path.name if root.is_file() else path.relative_to(root).as_posix()
        if patterns and not any(
            matches(relative, item) or ("/" not in item and fnmatch.fnmatchcase(path.name, item)) for item in patterns
        ):
            continue
        if mode == "glob":
            print(relative, flush=True)
            continue
        try:
            if mode == "count":
                count = sum(1 for kind, _, _ in grep_file(path, expression, 0) if kind == "match")
                if count:
                    sys.stdout.write(f"{path}\0{count}\n")
                    sys.stdout.flush()
                continue
            for kind, number, line in grep_file(path, expression, arguments.get("context", 0)):
                print(
                    json.dumps(
                        {
                            "type": kind,
                            "data": {"path": {"text": str(path)}, "line_number": number, "lines": {"text": line}},
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
        except (UnicodeError, PermissionError, IsADirectoryError):
            continue


if __name__ == "__main__":
    # pipe text encoding is explicit even under legacy Windows code pages.
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        main()
    except (ValueError, OSError, re.error) as exc:
        print(type(exc).__name__, file=sys.stderr)
        raise SystemExit(2) from None
