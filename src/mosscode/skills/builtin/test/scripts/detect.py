"""Suggest test commands for the project in the working directory; prints a JSON array.

Each item is {"kind", "command", "evidence"}. Only reads files; never runs anything. Standard library only.
"""

import configparser
import json
from pathlib import Path
import sys
import tomllib


def _text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _python(root: Path) -> dict | None:
    evidence = []
    try:
        pyproject = tomllib.loads(_text(root / "pyproject.toml")) if (root / "pyproject.toml").is_file() else {}
    except tomllib.TOMLDecodeError:
        pyproject = {}
    if "pytest" in pyproject.get("tool", {}):
        evidence.append("pyproject.toml 含 [tool.pytest]")
    if (root / "pytest.ini").is_file():
        evidence.append("pytest.ini")
    for name, section in (("tox.ini", "pytest"), ("setup.cfg", "tool:pytest")):
        if (root / name).is_file():
            parser = configparser.ConfigParser(interpolation=None)
            try:
                parser.read_string(_text(root / name))
            except configparser.Error:
                continue
            if parser.has_section(section):
                evidence.append(f"{name} 含 [{section}]")
    if (root / "tests").is_dir():
        evidence.append("tests/ 目录")
    if not evidence:
        return None
    python = "python"
    for candidate in (".venv/Scripts/python.exe", ".venv/bin/python"):
        if (root / candidate).is_file():
            python = candidate.removesuffix(".exe")
            evidence.append(f"{candidate} 存在")
            break
    return {"kind": "python", "command": f"{python} -m pytest", "evidence": "；".join(evidence)}


def _node(root: Path) -> dict | None:
    try:
        package = json.loads(_text(root / "package.json")) if (root / "package.json").is_file() else {}
    except ValueError:
        return None
    script = package.get("scripts", {}).get("test") if isinstance(package, dict) else None
    if not isinstance(script, str) or not script.strip() or "no test specified" in script:
        return None  # npm init's placeholder fails on purpose; it is not a test suite.
    for lock, tool in (("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn")):
        if (root / lock).is_file():
            return {"kind": "node", "command": f"{tool} test", "evidence": f"package.json 的 scripts.test；{lock}"}
    return {"kind": "node", "command": "npm test", "evidence": "package.json 的 scripts.test"}


def _jvm(root: Path) -> dict | None:
    if (root / "pom.xml").is_file():
        return {"kind": "maven", "command": "mvn test", "evidence": "pom.xml"}
    for name in ("build.gradle", "build.gradle.kts"):
        if (root / name).is_file():
            wrapper = (root / "gradlew").is_file() or (root / "gradlew.bat").is_file()
            return {"kind": "gradle", "command": "./gradlew test" if wrapper else "gradle test",
                    "evidence": name + ("；gradlew" if wrapper else "")}
    return None


def detect(root: Path) -> list[dict]:
    found = [_python(root), _node(root)]
    if (root / "go.mod").is_file():
        found.append({"kind": "go", "command": "go test ./...", "evidence": "go.mod"})
    if (root / "Cargo.toml").is_file():
        found.append({"kind": "rust", "command": "cargo test", "evidence": "Cargo.toml"})
    found.append(_jvm(root))
    return [item for item in found if item is not None]


def main() -> None:
    sys.stdin.read()  # The arguments object; this tool takes none.
    print(json.dumps(detect(Path.cwd()), ensure_ascii=False))


if __name__ == "__main__":
    main()
