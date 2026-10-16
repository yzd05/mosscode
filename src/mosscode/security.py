"""Application-level hard guards, independent of configurable permissions."""

from pathlib import Path
import re
import shlex


class SecurityError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def resolve_path(root: Path, value: str | Path) -> Path:
    """Resolve links and relative parts; no project boundary check."""
    if not str(value) or "\0" in str(value):
        raise SecurityError("invalid_path", "路径不能为空或包含空字符。")
    try:
        path = Path(value).expanduser()
        return (path if path.is_absolute() else root / path).resolve()
    except (OSError, RuntimeError):
        raise SecurityError("invalid_path", "无法安全解析目标路径或目录链接。") from None


def inside(root: Path, path: Path) -> bool:
    # root is the canonical anchor captured at session creation. Re-resolving
    # it here would silently move the boundary if that directory became a link.
    return path.is_relative_to(root)


def project_path(root: Path, value: str | Path) -> Path:
    path = resolve_path(root, value)
    if not inside(root, path):
        raise SecurityError("path_blocked", "目标解析后位于项目目录之外，此操作不能授权。")
    return path


# These are known-danger signatures, not a shell parser or an OS sandbox.
# Keep static expressions bounded; configuration cannot replace them.
def _compile(entries):
    return tuple((re.compile(pattern, re.IGNORECASE), reason) for pattern, reason in entries)


# System-level damage outside any project: never authorizable.
CATASTROPHIC_COMMANDS = _compile((
    (r"\b(?:rm|remove-item|ri|rmdir|rd|del|erase)\b[^\r\n;|&]*\s['\"]?"
     r"(?:/\*?|~[\\/]?\*?|\$home[\\/]?\*?|\$env:userprofile[\\/]?\*?|%userprofile%[\\/]?\*?|[a-z]:[\\/]?\*?)['\"]?(?:\s|$|[;|&])",
     "删除文件系统根目录或用户主目录"),
    (r"--no-preserve-root\b", "关闭根目录保护"),
    (r"\b(?:format-volume|clear-disk|initialize-disk|diskpart|mkfs(?:\.[\w]+)?)\b", "磁盘格式化或分区操作"),
    (r"\bformat(?:\.com)?\s+[a-z]:", "磁盘格式化"),
    (r"\bdd\b[^\r\n;|&]*\bof=/dev/(?:sd|hd|nvme|vd|mmc|disk)", "覆盖块设备"),
    (r">\s*/dev/(?:sd|hd|nvme|vd|mmc|disk)", "覆盖块设备"),
    (r"\b(?:shutdown|reboot|halt|poweroff|restart-computer|stop-computer)\b", "关机或重启"),
    (r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&", "进程炸弹"),
))

# Destructive but sometimes intended: always confirmed, never granted by rules or modes.
RISKY_COMMANDS = _compile((
    (r"\b(?:rm|remove-item|ri)\b[^\r\n;|&]*\s-(?:[a-z]*r[a-z]*f[a-z]*|[a-z]*f[a-z]*r[a-z]*|recurse)\b", "递归强制删除"),
    (r"\b(?:rm|remove-item|ri)\b[^\r\n;|&]*\s-(?:r|recursive)\b[^\r\n;|&]*\s-(?:f|force)\b", "递归强制删除"),
    (r"\b(?:rm|remove-item|ri)\b[^\r\n;|&]*\s-(?:f|force)\b[^\r\n;|&]*\s-(?:r|recursive)\b", "递归强制删除"),
    (r"\b(?:rmdir|rd|del|erase)\b[^\r\n;|&]*\s/s\b", "递归删除"),
    (r"\bgit\b[^\r\n;|&]*\breset\s+[^\r\n;|&]*--hard\b", "丢弃 Git 改动"),
    (r"\bgit\b[^\r\n;|&]*\bclean\s+[^\r\n;|&]*-(?:[a-z]*f[a-z]*|force)\b", "强制清理 Git 文件"),
    (r"\bgit\b[^\r\n;|&]*\bpush\s+[^\r\n;|&]*(?:--force(?:-with-lease)?\b|-f\b)", "强制推送"),
    (r"\b(?:invoke-expression|iex)\b", "动态表达式执行"),
    (r"\b(?:powershell|pwsh)(?:\.exe)?\b[^\r\n;|&]*\s-(?:e|en|enc|encodedcommand)\b", "编码命令执行"),
))


def check_command(command: str) -> None:
    if "\0" in command:
        raise SecurityError("invalid_command", "命令不能包含空字符。")
    for expression, reason in CATASTROPHIC_COMMANDS:
        if expression.search(command):
            raise SecurityError("dangerous_command", f"内置黑名单拒绝：{reason}。此操作不能授权。")


def command_risk(command: str) -> str | None:
    """Reason a command needs confirmation every time, if any."""
    return next((reason for expression, reason in RISKY_COMMANDS if expression.search(command)), None)


def command_words(command: str) -> list[str] | None:
    try:
        return shlex.split(command)
    except ValueError:
        return None


def command_segments(command: str) -> list[str]:
    """Rough split of a compound command, for deny rules only (over-matching is safe there)."""
    return [part.strip() for part in re.split(r"\$\(|[;&|\r\n`(){}]+", command) if part.strip()]


def starts_with(command: str, prefix: str) -> bool:
    """Word-wise prefix: `pytest` matches `pytest -q` but not `pytestx`."""
    words, head = command_words(command), command_words(prefix)
    if words is None:
        words = command.split()
    return bool(head) and words[:len(head)] == head


# Inspection commands that run without approval. Conservative on purpose: every
# argument must stay inside the project and options that run programs are refused.
READ_ONLY_GIT = {"status", "diff", "log", "show", "blame", "rev-parse", "ls-files", "describe", "shortlog"}
GIT_LISTING = {"branch", "remote"}
LISTING_FLAGS = {"-a", "-r", "-v", "-vv", "--list", "--show-current", "--all", "--remotes", "--verbose"}
READ_ONLY_PROGRAMS = {"ls", "pwd", "cat", "head", "tail", "wc", "stat", "tree", "which", "grep", "egrep", "fgrep",
                      "rg", "find", "get-childitem", "get-content", "get-location", "select-string", "test-path",
                      "get-item", "gci", "gc", "sls", "dir"}
FIND_ACTIONS = {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls"}


def _short_flag(word: str, letters: str) -> bool:
    return word.startswith("-") and not word.startswith("--") and any(c in word[1:] for c in letters)


def _forbidden_option(program: str, word: str) -> bool:
    if program == "git":
        return word.startswith(("--output", "--ext-diff"))
    if program == "tail":
        return word.startswith("--follow") or _short_flag(word, "fF")
    if program == "rg":  # --pre and --search-zip run other programs.
        return word == "--pre" or word.startswith(("--pre=", "--search-zip")) or _short_flag(word, "z")
    if program == "find":
        return word in FIND_ACTIONS
    return False


def _inside_argument(word: str) -> bool:
    value = word.split("=", 1)[1] if word.startswith("-") and "=" in word else word
    if word.startswith("-") and value is word:
        return True
    if value.startswith(("/", "\\", "~")) or re.match(r"[A-Za-z]:", value):
        return False
    return ".." not in re.split(r"[\\/]", value)


def read_only_command(command: str) -> bool:
    words = command_words(command) if simple_command(command) else None
    if not words:
        return False
    program, arguments = words[0].lower(), words[1:]
    if program == "git":
        if not arguments:
            return False
        if arguments[0] in GIT_LISTING:
            return all(word in LISTING_FLAGS for word in arguments[1:])
        if arguments[0] not in READ_ONLY_GIT:
            return False  # Also refuses global options such as `git -c`.
    elif program not in READ_ONLY_PROGRAMS:
        return False
    return all(not _forbidden_option(program, word) and _inside_argument(word) for word in arguments)


# Never offered as a prefix grant: they run whatever follows.
WRAPPERS = {"sudo", "doas", "env", "xargs", "nohup", "timeout", "time", "nice", "exec", "eval", "source", ".",
            "bash", "sh", "zsh", "fish", "cmd", "pwsh", "powershell", "start", "call", "invoke-expression", "iex"}
# Need a following word: `python -m pytest`, `deno test`, `npm run test`.
INTERPRETERS = {"python", "python3", "py", "node", "ruby", "perl", "php", "deno", "bun"}
MULTI_COMMANDS = {"git", "npm", "pnpm", "yarn", "npx", "uv", "uvx", "pipx", "poetry", "pip", "cargo", "go", "dotnet",
                  "docker", "kubectl", "make", "gradle", "mvn", "conda"}


def command_prefix(command: str) -> str | None:
    """The leading words worth remembering, e.g. `npm run test` or `python -m pytest`."""
    words = command_words(command) if simple_command(command) else None
    if not words or words[0].lower() in WRAPPERS:
        return None
    prefix = []
    for word in words:
        if len(prefix) == 3:
            break
        if word == "-m" and prefix and prefix[-1].lower() in {"python", "python3", "py"}:
            prefix.append(word)
            continue
        if word.startswith("-") or re.search(r"[/\\.=:*?~$@%\"'\[\]]", word):
            break
        prefix.append(word)
    if not prefix or (prefix[-1] == "-m"):
        return None
    first = prefix[0].lower()
    if len(prefix) == 1 and (first in INTERPRETERS or first in MULTI_COMMANDS):
        return None
    if first in {"python", "python3", "py"} and prefix[1:2] != ["-m"]:
        return None
    return " ".join(prefix)


def simple_command(command: str) -> bool:
    """Conservatively exclude compound/dynamic shell syntax from glob allows."""
    quote = None
    for char in command:
        if char in "\r\n`$" or (ord(char) < 32 and char != "\t"):
            return False
        if quote:
            if char == quote:
                quote = None
            # Backslash quoting differs across shells; prefer confirmation.
            elif char == "\\" and quote == '"':
                return False
        elif char in "'\"":
            quote = char
        elif char in ";&|><(){}\\":
            return False
    return quote is None
