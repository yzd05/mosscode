"""Export the public repository from this development repository.

Only the allowlisted paths are copied; specs, research notes, acceptance material, local scripts and
course material stay here. Text files are written with LF line endings, then every exported file is
scanned for local paths, private references, credentials and personal e-mail addresses, and the
README/reference links are checked in the exported tree.

    python scripts/export_public.py ../mosscode-public            export the last commit (HEAD)
    python scripts/export_public.py ../mosscode-public --ref v0.1.0
    python scripts/export_public.py ../mosscode-public --worktree preview uncommitted changes
    python scripts/export_public.py ../mosscode-public --pytest   also run the full suite there

The target must be missing, empty, or a git checkout of the public repository: everything in it
except .git is replaced. Nothing is committed or pushed; review with git status/diff there.
"""

from __future__ import annotations

import argparse
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

ALLOW = [
    "src",
    "tests",
    "docs/reference",
    "docs/pypi.md",
    ".github",
    "README.md",
    "LICENSE",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "AGENTS.md",
    "SECURITY.md",
    "pyproject.toml",
    "MANIFEST.in",
    "requirements-lock.txt",
    "config.example.yaml",
    "mcp.example.yaml",
    "permissions.example.yaml",
]
# Inside the allowlist but tied to development-only material.
EXCLUDE = {"tests/test_cache_probe.py"}  # Tests scripts/probe_prompt_cache.py, which stays here.

# Written instead of the development .gitignore, which names private material.
GITIGNORE = """\
# Python
.venv/
__pycache__/
*.egg-info/
.pytest_cache/
.ruff_cache/
dist/
build/

# Test runs
.test-artifacts/

# Local configuration: provider keys, machine-specific MCP servers and grants
config.yaml
mcp.yaml
.mosscode/mcp.yaml
.mosscode/permissions.local.yaml
.env
"""

# Anything that points back at this machine or at material that is not exported.
PRIVATE = re.compile(
    r"code-storage|PyCharm[\\/]MossCode"
    r"|docs/(?:specs|research|ui)\b|docs/acceptance|acceptance_setup|verify_(?:keys|resize|inline)"
    r"|code-spec|_local/|\brtk\b|\bLEGION\b",
    re.I,
)
SECRET = re.compile(
    r"sk-(?:ant-)?[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{30,}|xox[bp]-[A-Za-z0-9-]{10,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----\s*\n\s*[A-Za-z0-9+/]{20,}"
)
EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.(?:com|net|org|cn|io|dev|edu|me)\b")
EMAIL_OK = re.compile(r"@(?:example\.(?:com|org|net)|users\.noreply\.github\.com)$|^a@b\.com$|^git@github\.com$")
# Files that describe the patterns above rather than contain them, and the fake keys tests use.
SCAN_EXEMPT = {"src/mosscode/memory.py", "docs/reference/memory.md", "tests/test_memory.py"}
FAKE_SECRETS = {"sk-test-do-not-show-123"}


def git(*args: str) -> bytes:
    return subprocess.run(["git", "-C", str(ROOT), *args], check=True, capture_output=True).stdout


def from_ref(ref: str) -> dict[str, bytes]:
    archive = git("archive", "--format=tar", ref, "--", *ALLOW)
    files = {}
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        for member in tar:
            if member.isfile():
                files[member.name] = tar.extractfile(member).read()
    return files


def from_worktree() -> dict[str, bytes]:
    names = git("ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", *ALLOW).split(b"\0")
    files = {}
    for name in filter(None, (n.decode() for n in names)):
        path = ROOT / name
        if path.is_file() and "__pycache__" not in path.parts:
            files[name] = path.read_bytes()
    return files


def normalize(data: bytes) -> bytes:
    if b"\0" in data:
        return data
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return data
    return data.replace(b"\r\n", b"\n")


def scan(files: dict[str, bytes]) -> list[str]:
    problems = []
    for name, data in sorted(files.items()):
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            problems.append(f"{name}: not UTF-8 text")
            continue
        for number, line in enumerate(text.splitlines(), 1):
            hits = [m.group(0) for m in PRIVATE.finditer(line)]
            if name not in SCAN_EXEMPT:
                hits += [m.group(0) for m in SECRET.finditer(line) if m.group(0) not in FAKE_SECRETS]
            hits += [m.group(0) for m in EMAIL.finditer(line) if not EMAIL_OK.search(m.group(0))]
            problems += [f"{name}:{number}: {hit}" for hit in hits]
    return problems


def prepare(target: Path) -> None:
    target = target.resolve()
    if target == ROOT or ROOT in target.parents or target in ROOT.parents:
        sys.exit(f"refusing to export into or around the development repository: {target}")
    if target.exists():
        entries = [p for p in target.iterdir() if p.name != ".git"]
        if entries and not (target / ".git").exists():
            sys.exit(f"{target} is not empty and not a git checkout; choose another directory")
        for entry in entries:
            shutil.rmtree(entry) if entry.is_dir() and not entry.is_symlink() else entry.unlink()
    target.mkdir(parents=True, exist_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export the public repository (see the module docstring).")
    parser.add_argument("target", type=Path)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--ref", default="HEAD", help="commit or tag to export (default HEAD)")
    source.add_argument("--worktree", action="store_true", help="export the working tree, uncommitted changes included")
    parser.add_argument("--pytest", action="store_true", help="run the full test suite in the exported tree")
    args = parser.parse_args(argv)

    files = from_worktree() if args.worktree else from_ref(args.ref)
    files = {name: data for name, data in files.items() if name not in EXCLUDE}
    missing = [p for p in ALLOW if not any(n == p or n.startswith(p + "/") for n in files)]
    if missing:
        sys.exit(f"allowlisted paths missing from the source: {', '.join(missing)}")
    files = {name: normalize(data) for name, data in files.items()}
    files[".gitignore"] = GITIGNORE.encode()

    problems = scan(files)
    if problems:
        print("export stopped; remove these or extend the exemptions deliberately:", file=sys.stderr)
        print("\n".join(f"  {p}" for p in problems), file=sys.stderr)
        return 1

    prepare(args.target)
    for name, data in files.items():
        path = args.target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    print(f"exported {len(files)} files from {'the working tree' if args.worktree else args.ref} to {args.target}")

    # The exported src comes first on the path, ahead of this repository's editable install.
    env = {**os.environ, "PYTHONPATH": str((args.target / "src").resolve())}
    # Collecting every test catches imports of anything that was not exported.
    checks = [["--collect-only", "-q"], ["tests/test_docs.py"]] + ([[]] if args.pytest else [])
    for extra in checks:
        command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--rootdir", ".", *extra]
        if subprocess.run(command, cwd=args.target, env=env).returncode:
            return 1
    print("next: review with git status / git diff in the target, then commit and push from there")
    return 0


if __name__ == "__main__":
    sys.exit(main())
