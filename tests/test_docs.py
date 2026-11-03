"""README stays a four-part entry point; its links and the reference docs' links resolve."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCS = [ROOT / "README.md", *sorted((ROOT / "docs/reference").glob("*.md"))]


def anchors(path: Path) -> set[str]:
    # GitHub-style slugs: lower case, punctuation removed, spaces become hyphens.
    return {
        re.sub(r"[^\w\- ]", "", h.strip().lower()).replace(" ", "-")
        for h in re.findall(r"^#+ (.+)$", path.read_text(encoding="utf-8"), re.M)
    }


def test_readme_has_install_configure_use_architecture():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert re.findall(r"^## (.+)$", text, re.M) == ["安装", "配置", "使用", "架构"]


@pytest.mark.parametrize("path", DOCS, ids=lambda p: p.name)
def test_relative_links_resolve(path):
    for target in re.findall(r"\]\(([^)\s]+)\)", path.read_text(encoding="utf-8")):
        if target.startswith(("http://", "https://")):
            continue
        file_part, _, anchor = target.partition("#")
        resolved = (path.parent / file_part).resolve() if file_part else path
        assert resolved.exists(), target
        if anchor and resolved.suffix == ".md":
            assert anchor in anchors(resolved), target
