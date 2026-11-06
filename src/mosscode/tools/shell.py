"""Pick the command shell once per registry: Git Bash on Windows when present."""

import os
import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Shell:
    kind: str  # "bash", "sh" or "powershell"
    path: str
    label: str  # Shown to the model and the user.

    def args(self, command: str) -> list[str]:
        if self.kind == "powershell":
            # On its own line, so PowerShell error positions show only the command.
            prelude = "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false); $OutputEncoding=[Console]::OutputEncoding; $LASTEXITCODE=$null\n"
            suffix = "\n$mosscodeCommandSucceeded=$?; if ($null -ne $LASTEXITCODE) { exit $LASTEXITCODE }; if (-not $mosscodeCommandSucceeded) { exit 1 }"
            return [self.path, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", prelude + command + suffix]
        if self.kind == "bash" and os.name == "nt":
            return [self.path, "--noprofile", "--norc", "-c", f'eval "${COMMAND_VARIABLE}"']
        if self.kind == "bash":
            return [self.path, "--noprofile", "--norc", "-c", command]
        return [self.path, "-c", command]

    def env(self, command: str) -> dict[str, str] | None:
        # MSYS bash re-parses its Windows command line Cygwin-style, which
        # collapses `\\` inside quoted arguments; the environment is passed verbatim.
        if self.kind == "bash" and os.name == "nt":
            return {**os.environ, COMMAND_VARIABLE: command}
        return None


COMMAND_VARIABLE = "MOSSCODE_COMMAND"


class ShellNotFound(ValueError):
    pass


def _is_wsl(path: Path) -> bool:
    # System32\bash.exe starts WSL, a different filesystem and toolchain.
    system = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    try:
        return path.resolve().is_relative_to(system.resolve())
    except OSError:
        return True


def _unshim(path: Path) -> Path:
    # Scoop shims are tiny launchers; the real target is in <name>.shim.
    shim = path.with_suffix(".shim")
    try:
        for line in shim.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "path":
                return Path(value.strip().strip('"'))
    except (OSError, UnicodeError):
        pass
    return path


def git_bash() -> Path | None:
    candidates = []
    git = shutil.which("git")
    if git:
        # git.exe lives in <root>\cmd, <root>\bin or <root>\mingw64\bin.
        candidates.extend(parent / "bin" / "bash.exe" for parent in _unshim(Path(git)).resolve().parents[:3])
    bash = shutil.which("bash")
    if bash:
        bash = _unshim(Path(bash)).resolve()
        # Prefer the <root>\bin wrapper: it puts /usr/bin and /mingw64/bin on PATH.
        if bash.parent.name.lower() == "bin" and bash.parent.parent.name.lower() == "usr":
            candidates.append(bash.parents[2] / "bin" / "bash.exe")
        candidates.append(bash)
    for variable in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        if os.environ.get(variable):
            candidates.append(Path(os.environ[variable]) / "Git" / "bin" / "bash.exe")
    if os.environ.get("LOCALAPPDATA"):
        candidates.append(Path(os.environ["LOCALAPPDATA"]) / "Programs" / "Git" / "bin" / "bash.exe")
    return next((path for path in candidates if path.is_file() and not _is_wsl(path)), None)


def _powershell() -> Shell | None:
    path = shutil.which("pwsh") or shutil.which("powershell")
    return Shell("powershell", path, "PowerShell") if path else None


def resolve_shell(preference: str = "auto") -> Shell:
    preference = (preference or "auto").strip()
    lowered = preference.lower()
    if lowered == "powershell":
        shell = _powershell()
        if shell is None:
            raise ShellNotFound("配置要求使用 PowerShell，但找不到 pwsh 或 powershell。")
        return shell
    if lowered in {"auto", "bash"}:
        if os.name == "nt":
            found = git_bash()
            if found is not None:
                return Shell("bash", str(found), "Git Bash")
            if lowered == "bash":
                raise ShellNotFound(
                    "配置要求使用 bash，但没有找到 Git Bash；可安装 Git for Windows，或在 shell 中填写 bash.exe 的路径。"
                )
            shell = _powershell()
            if shell is None:
                raise ShellNotFound("找不到 Git Bash，也找不到 PowerShell。")
            return shell
        found = shutil.which("bash")
        if found:
            return Shell("bash", found, "bash")
        if lowered == "bash":
            raise ShellNotFound("配置要求使用 bash，但 PATH 中没有 bash。")
        return Shell("sh", "/bin/sh", "sh")
    path = Path(preference).expanduser()
    if not path.is_file():
        raise ShellNotFound(f"shell 配置的路径不存在：{path}")
    if os.name == "nt" and _is_wsl(path):
        raise ShellNotFound("shell 不支持 System32 下的 WSL bash；请填写 Git Bash 的 bash.exe。")
    return Shell("bash", str(path), "Git Bash" if os.name == "nt" else "bash")
