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


class MemoryKeyring:
    """A credential manager in memory, in place of the system's for every test."""

    priority = 1

    def __init__(self):
        self.entries: dict[tuple[str, str], str] = {}

    def get_password(self, service, name):
        return self.entries.get((service, name))

    def set_password(self, service, name, value):
        self.entries[(service, name)] = value

    def delete_password(self, service, name):
        from keyring.errors import PasswordDeleteError

        if self.entries.pop((service, name), None) is None:
            raise PasswordDeleteError(name)


@pytest.fixture(autouse=True)
def memory_keyring(monkeypatch):
    """Tests never read or write the real credential manager, and the keys one test registers stay in it."""
    import keyring

    from mosscode import credentials

    backend = MemoryKeyring()
    monkeypatch.setattr(keyring, "get_keyring", lambda: backend)
    for action in ("get_password", "set_password", "delete_password"):
        monkeypatch.setattr(keyring, action, getattr(backend, action))
    from mosscode import command_env

    credentials.forget_all()
    command_env.reset()
    yield backend
    credentials.forget_all()
    command_env.reset()
