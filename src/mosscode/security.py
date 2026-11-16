"""Application-level hard guards, independent of configurable permissions."""

import functools
import ipaddress
import re
import shlex
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


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


# These are known-danger signatures, not an OS sandbox; they also run on each parsed subcommand.
# Keep static expressions bounded; configuration cannot replace them.
def _compile(entries):
    return tuple((re.compile(pattern, re.IGNORECASE), reason) for pattern, reason in entries)


# System-level damage outside any project: never authorizable.
CATASTROPHIC_COMMANDS = _compile(
    (
        (
            r"\b(?:rm|remove-item|ri|rmdir|rd|del|erase)\b[^\r\n;|&]*\s['\"]?"
            r"(?:/\*?|~[\\/]?\*?|\$home[\\/]?\*?|\$env:userprofile[\\/]?\*?|%userprofile%[\\/]?\*?|[a-z]:[\\/]?\*?)['\"]?(?:\s|$|[;|&])",
            "删除文件系统根目录或用户主目录",
        ),
        (r"--no-preserve-root\b", "关闭根目录保护"),
        (r"\b(?:format-volume|clear-disk|initialize-disk|diskpart|mkfs(?:\.[\w]+)?)\b", "磁盘格式化或分区操作"),
        (r"\bformat(?:\.com)?\s+[a-z]:", "磁盘格式化"),
        (r"\bdd\b[^\r\n;|&]*\bof=/dev/(?:sd|hd|nvme|vd|mmc|disk)", "覆盖块设备"),
        (r">\s*/dev/(?:sd|hd|nvme|vd|mmc|disk)", "覆盖块设备"),
        (r"\b(?:shutdown|reboot|halt|poweroff|restart-computer|stop-computer)\b", "关机或重启"),
        (r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&", "进程炸弹"),
    )
)

# Destructive but sometimes intended: always confirmed, never granted by rules or modes.
RISKY_COMMANDS = _compile(
    (
        (
            r"\b(?:rm|remove-item|ri)\b[^\r\n;|&]*\s-(?:[a-z]*r[a-z]*f[a-z]*|[a-z]*f[a-z]*r[a-z]*|recurse)\b",
            "递归强制删除",
        ),
        (r"\b(?:rm|remove-item|ri)\b[^\r\n;|&]*\s-(?:r|recursive)\b[^\r\n;|&]*\s-(?:f|force)\b", "递归强制删除"),
        (r"\b(?:rm|remove-item|ri)\b[^\r\n;|&]*\s-(?:f|force)\b[^\r\n;|&]*\s-(?:r|recursive)\b", "递归强制删除"),
        (r"\b(?:rmdir|rd|del|erase)\b[^\r\n;|&]*\s/s\b", "递归删除"),
        (r"\bgit\b[^\r\n;|&]*\breset\s+[^\r\n;|&]*--hard\b", "丢弃 Git 改动"),
        (r"\bgit\b[^\r\n;|&]*\bclean\s+[^\r\n;|&]*-(?:[a-z]*f[a-z]*|force)\b", "强制清理 Git 文件"),
        (r"\bgit\b[^\r\n;|&]*\bpush\s+[^\r\n;|&]*(?:--force(?:-with-lease)?\b|-f\b)", "强制推送"),
        (r"\b(?:invoke-expression|iex)\b", "动态表达式执行"),
        (r"\b(?:powershell|pwsh)(?:\.exe)?\b[^\r\n;|&]*\s-(?:e|en|enc|encodedcommand)\b", "编码命令执行"),
    )
)


@functools.cache
def _bash_language():
    try:
        import tree_sitter
        import tree_sitter_bash
    except ImportError:
        return None
    return tree_sitter.Language(tree_sitter_bash.language())


def _static_word(node) -> str | None:
    """A word's value with bash quoting removed, or None when it depends on an expansion."""
    kind, text = node.type, node.text.decode("utf-8", "replace")
    if kind == "command_name":
        return _static_word(node.named_children[0]) if node.named_child_count == 1 else None
    if kind in {"word", "number"}:
        return re.sub(r"\\(.)", r"\1", text, flags=re.DOTALL)
    if kind == "raw_string":
        return text[1:-1]
    if kind == "string":
        if any(child.type != "string_content" for child in node.named_children):
            return None
        return re.sub(r'\\([$`"\\\n])', r"\1", text[1:-1])
    if kind == "concatenation":
        parts = [
            _static_word(child) if child.is_named else child.text.decode("utf-8", "replace") for child in node.children
        ]
        return None if None in parts else "".join(parts)
    return None


def subcommands(command: str) -> list[str]:
    """Every simple command in a bash command line, nested ones included, with quoting taken out of its words:
    `r''m -rf /` and `echo $("rm" -rf /)` both yield `rm -rf /`. A word that depends on an expansion keeps
    its source text. Empty when tree-sitter is not installed.

    Only ever used for extra checks next to the raw text, so a parse that does not fit the actual shell
    (PowerShell) can add a confirmation but never remove one.
    """
    language = _bash_language()
    if language is None or not command.strip():
        return []
    from tree_sitter import Parser

    found, stack = [], [Parser(language).parse(command.encode("utf-8")).root_node]
    while stack:
        node = stack.pop()
        if node.type == "command":
            words = [node.child_by_field_name("name"), *node.children_by_field_name("argument")]
            values = []
            for word in words:
                if word is not None:
                    value = _static_word(word)
                    values.append(word.text.decode("utf-8", "replace") if value is None else value)
            if values:
                found.append(" ".join(values))
        stack.extend(reversed(node.children))
    return found


def check_command(command: str) -> None:
    if "\0" in command:
        raise SecurityError("invalid_command", "命令不能包含空字符。")
    candidates = (command, *subcommands(command))
    for expression, reason in CATASTROPHIC_COMMANDS:
        if any(expression.search(candidate) for candidate in candidates):
            raise SecurityError("dangerous_command", f"内置黑名单拒绝：{reason}。此操作不能授权。")


def command_risk(command: str) -> str | None:
    """Reason a command needs confirmation every time, if any."""
    candidates = (command, *subcommands(command))
    return next(
        (
            reason
            for expression, reason in RISKY_COMMANDS
            if any(expression.search(candidate) for candidate in candidates)
        ),
        None,
    )


def command_words(command: str) -> list[str] | None:
    try:
        return shlex.split(command)
    except ValueError:
        return None


def command_segments(command: str) -> list[str]:
    """Parts of a compound command, for deny rules and hook conditions only (over-matching is safe there):
    a rough split of the text plus the parsed subcommands."""
    rough = [part.strip() for part in re.split(r"\$\(|[;&|\r\n`(){}]+", command) if part.strip()]
    return list(dict.fromkeys((*rough, *subcommands(command))))


def starts_with(command: str, prefix: str) -> bool:
    """Word-wise prefix: `pytest` matches `pytest -q` but not `pytestx`."""
    words, head = command_words(command), command_words(prefix)
    if words is None:
        words = command.split()
    return bool(head) and words[: len(head)] == head


# Inspection commands that run without approval. Conservative on purpose: every
# argument must stay inside the project and options that run programs are refused.
READ_ONLY_GIT = {"status", "diff", "log", "show", "blame", "rev-parse", "ls-files", "describe", "shortlog"}
GIT_LISTING = {"branch", "remote"}
LISTING_FLAGS = {"-a", "-r", "-v", "-vv", "--list", "--show-current", "--all", "--remotes", "--verbose"}
READ_ONLY_PROGRAMS = {
    "ls",
    "pwd",
    "cat",
    "head",
    "tail",
    "wc",
    "stat",
    "tree",
    "which",
    "grep",
    "egrep",
    "fgrep",
    "rg",
    "find",
    "get-childitem",
    "get-content",
    "get-location",
    "select-string",
    "test-path",
    "get-item",
    "gci",
    "gc",
    "sls",
    "dir",
}
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
WRAPPERS = {
    "sudo",
    "doas",
    "env",
    "xargs",
    "nohup",
    "timeout",
    "time",
    "nice",
    "exec",
    "eval",
    "source",
    ".",
    "bash",
    "sh",
    "zsh",
    "fish",
    "cmd",
    "pwsh",
    "powershell",
    "start",
    "call",
    "invoke-expression",
    "iex",
}
# Need a following word: `python -m pytest`, `deno test`, `npm run test`.
INTERPRETERS = {"python", "python3", "py", "node", "ruby", "perl", "php", "deno", "bun"}
MULTI_COMMANDS = {
    "git",
    "npm",
    "pnpm",
    "yarn",
    "npx",
    "uv",
    "uvx",
    "pipx",
    "poetry",
    "pip",
    "cargo",
    "go",
    "dotnet",
    "docker",
    "kubectl",
    "make",
    "gradle",
    "mvn",
    "conda",
}


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


# Proxy tools in fake-IP mode (Clash, Surge) answer every DNS query from this benchmark range.
FAKE_IP = ipaddress.ip_network("198.18.0.0/15")
NAT64 = ipaddress.ip_network("64:ff9b::/96")


def public_address(value: str) -> bool:
    """Whether web_fetch may connect to this address: not loopback, private, link-local or reserved."""
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        # Addresses that carry an IPv4 one are judged by it.
        embedded = address.ipv4_mapped or address.sixtofour or (address.teredo or (None, None))[1]
        if embedded is None and address in NAT64:
            embedded = ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
        if embedded is not None:
            address = embedded
    if address in FAKE_IP:
        return True
    return address.is_global and not address.is_multicast


def web_target(url: str) -> tuple[str, str]:
    """The https URL web_fetch requests and its lowercase host; local addresses are refused before any request."""
    try:
        parts = urlsplit(url.strip())
        host, port = parts.hostname, parts.port
    except ValueError:
        raise SecurityError("invalid_url", "URL 格式不正确。") from None
    if parts.scheme.lower() not in {"http", "https"} or not host:
        raise SecurityError("invalid_url", "web_fetch 只接受 http 和 https 地址。")
    if parts.username is not None or parts.password is not None:
        raise SecurityError("invalid_url", "URL 不能包含用户名或密码。")
    try:
        host = host.encode("idna").decode("ascii").rstrip(".")
    except UnicodeError:
        raise SecurityError("invalid_url", "URL 中的域名不合法。") from None
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    local = not public_address(host) if literal is not None else "." not in host or host == "localhost"
    if local or host.endswith(".localhost"):
        raise SecurityError(
            "local_blocked",
            "web_fetch 不访问本机和内网地址；需要访问本地服务时用 execute_command（例如 curl），按命令权限确认。",
        )
    netloc = f"[{host}]" if isinstance(literal, ipaddress.IPv6Address) else host
    if port not in (None, 80, 443):
        netloc += f":{port}"
    return urlunsplit(("https", netloc, parts.path or "/", parts.query, "")), host
