from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path_factory, monkeypatch):
    """Session transcripts and memory notes default to ~/.mosscode; tests never touch the real one."""
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


@pytest.fixture(autouse=True)
def git_identity(monkeypatch):
    """Merges and commits in tests work on machines (and CI runners) without a configured git identity."""
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "MossCode Tests")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "tests@example.com")
