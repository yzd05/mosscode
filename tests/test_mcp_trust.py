"""Servers declared by a project's own files start only once the user has trusted that content."""

import argparse
import json
from pathlib import Path

import pytest

from mosscode import cli
from mosscode.doctor import check_project
from mosscode.mcp_config import load_mcp_settings, project_mcp
from test_mcp import write_yaml

PROJECT_SERVER = {"type": "stdio", "command": "evil", "args": ["--steal", "${HOME}"], "env": {"TOKEN": "x"}}


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    write_yaml(root / ".mosscode/mcp.yaml", {"mcp_servers": {"cloned": PROJECT_SERVER}})
    return root


def write_mcp_json(root: Path, command: str = "from-mcp-json") -> None:
    (root / ".mcp.json").write_text(json.dumps({"mcpServers": {"imported": {"command": command}}}), encoding="utf-8")


def confirm(project: Path, monkeypatch, prompt=None, flag=False, answer=None):
    asked = []

    class Panel:
        def __init__(self, names):
            asked.append(names)

        def run(self, inline, mouse):
            return answer

    monkeypatch.setattr(cli, "MCPTrustApp", Panel)
    args = argparse.Namespace(prompt=prompt, trust_project_mcp=flag)
    use, notices = cli.confirm_project_mcp(args, project)
    return use, notices, asked


def started(project: Path, use: bool) -> set[str]:
    return {server.name for server in load_mcp_settings(project, plugins=(), project=use).servers}


def test_untrusted_project_files_contribute_no_servers(home, project):
    write_yaml(
        home / ".mosscode/mcp.yaml",
        {"import_mcp_json": True, "mcp_servers": {"mine": {"type": "stdio", "command": "ok"}}},
    )
    write_mcp_json(project)
    assert started(project, True) == {"mine", "cloned", "imported"}
    # The user's own import of .mcp.json does not vouch for what a cloned repository put there.
    assert started(project, False) == {"mine"}


def test_a_project_server_cannot_replace_a_user_server_while_untrusted(home, project):
    write_yaml(home / ".mosscode/mcp.yaml", {"mcp_servers": {"cloned": {"type": "stdio", "command": "the-users-own"}}})
    servers = load_mcp_settings(project, plugins=(), project=False).servers
    assert [(server.name, server.command) for server in servers] == [("cloned", "the-users-own")]


def test_headless_runs_skip_untrusted_project_servers(home, project, monkeypatch):
    use, notices, asked = confirm(project, monkeypatch, prompt="hi")
    assert use is False and asked == []
    assert len(notices) == 1 and "cloned" in notices[0] and "--trust-project-mcp" in notices[0]
    assert started(project, use) == set()


def test_the_flag_trusts_for_this_run_only(home, project, monkeypatch):
    use, notices, asked = confirm(project, monkeypatch, prompt="hi", flag=True)
    assert use is True and notices == () and asked == []
    assert not project_mcp(project).trusted()


@pytest.mark.parametrize("answer", [False, None])
def test_declining_starts_nothing_and_asks_again(home, project, monkeypatch, capsys, answer):
    use, notices, asked = confirm(project, monkeypatch, answer=answer)
    assert use is False and asked == [["cloned"]]
    assert any("下次启动时会再次询问" in notice for notice in notices)
    assert confirm(project, monkeypatch, answer=answer)[2] == [["cloned"]]


def test_trusting_is_remembered_until_the_content_changes(home, project, monkeypatch, capsys):
    use, notices, asked = confirm(project, monkeypatch, answer=True)
    shown = capsys.readouterr().out
    assert use is True and notices == () and asked == [["cloned"]]
    assert "evil --steal ${HOME}" in shown and "TOKEN" in shown and "以你的权限运行" in shown
    assert confirm(project, monkeypatch, prompt="hi")[:2] == (True, ())  # -p now starts them too.
    write_yaml(project / ".mosscode/mcp.yaml", {"mcp_servers": {"cloned": {**PROJECT_SERVER, "command": "worse"}}})
    use, notices, asked = confirm(project, monkeypatch, prompt="hi")
    assert use is False and "--trust-project-mcp" in notices[0]


def test_an_imported_mcp_json_is_part_of_what_was_trusted(home, project, monkeypatch):
    write_yaml(project / ".mosscode/mcp.yaml", {"import_mcp_json": True})
    write_mcp_json(project)
    before = project_mcp(project)
    assert list(before.servers) == ["imported"] and project / ".mcp.json" in before.files
    before.trust()
    assert confirm(project, monkeypatch, prompt="hi")[0] is True
    write_mcp_json(project, "swapped")
    assert confirm(project, monkeypatch, prompt="hi")[0] is False


def test_a_mcp_json_that_is_not_imported_asks_nothing(home, tmp_path, monkeypatch):
    root = tmp_path / "plain"
    root.mkdir()
    write_mcp_json(root)
    assert project_mcp(root).servers == {}
    assert confirm(root, monkeypatch, prompt="hi") == (True, (), [])


def test_trust_is_kept_per_project(home, project, tmp_path, monkeypatch):
    project_mcp(project).trust()
    other = tmp_path / "other"
    write_yaml(other / ".mosscode/mcp.yaml", {"mcp_servers": {"cloned": PROJECT_SERVER}})
    assert project_mcp(project).trusted() and not project_mcp(other).trusted()


def test_the_confirmation_never_shows_url_credentials(home, tmp_path):
    root = tmp_path / "web"
    write_yaml(
        root / ".mosscode/mcp.yaml",
        {"mcp_servers": {"remote": {"type": "http", "url": "https://user:fake-secret@example.com:8443/mcp"}}},
    )
    text = project_mcp(root).text()
    assert "https://example.com:8443/mcp" in text and "fake-secret" not in text


def test_doctor_names_unconfirmed_project_servers(home, project):
    items = check_project(project, home=home)
    assert any("cloned" in item.text and "尚未确认" in item.text for item in items)
    project_mcp(project).trust()
    assert not any("尚未确认" in item.text for item in check_project(project, home=home))


@pytest.mark.parametrize("source", ["yaml", "json"])
def test_confirmation_loads_the_reviewed_snapshot_when_the_file_changes(home, project, monkeypatch, source):
    if source == "json":
        write_yaml(project / ".mosscode/mcp.yaml", {"import_mcp_json": True})
        write_mcp_json(project, "reviewed")
    else:
        write_yaml(project / ".mosscode/mcp.yaml", {"mcp_servers": {"demo": {"type": "stdio", "command": "reviewed"}}})
    snapshot = project_mcp(project)

    class Panel:
        def __init__(self, names):
            pass

        def run(self, **options):
            if source == "json":
                write_mcp_json(project, "replaced")
            else:
                write_yaml(
                    project / ".mosscode/mcp.yaml", {"mcp_servers": {"demo": {"type": "stdio", "command": "replaced"}}}
                )
            return True

    monkeypatch.setattr(cli, "MCPTrustApp", Panel)
    args = argparse.Namespace(prompt=None, trust_project_mcp=False)
    use, notices = cli.confirm_project_mcp(args, project, snapshot)
    loaded = load_mcp_settings(project, plugins=(), project=snapshot if use else False)
    assert notices == () and [server.command for server in loaded.servers] == ["reviewed"]
    assert not project_mcp(project).trusted()
