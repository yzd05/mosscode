"""A project's .mosscode/config.yaml is used only once the user has confirmed its present content."""

import argparse
import os
from pathlib import Path

import pytest
import yaml

from mosscode import cli
from mosscode.config import ConfigError, confirm_project, load_settings, project_confirmed, project_summary
from mosscode.teams import Member, TeamStore
from mosscode.teams.backends import WINDOWS_TERMINAL, member_arguments

PROVIDER = {
    "name": "gateway",
    "protocol": "openai-chat-completions",
    "model": "some-model",
    "base_url": "https://llm.example.net/v1",
    "api_key_env": "MOSS_TEST_KEY",
}


def write(path: Path, *providers: dict, **top) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"providers": list(providers or (PROVIDER,)), **top}), encoding="utf-8")
    return path


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.chdir(root)
    return root


@pytest.fixture
def project(repo):
    return write(repo / ".mosscode" / "config.yaml")


# The record ---------------------------------------------------------------------------------------


def test_a_project_file_is_confirmed_for_its_present_content_only(project):
    assert not project_confirmed(load_settings(project))
    confirm_project(load_settings(project))
    assert project_confirmed(load_settings(project))
    # The same provider, now sending the user's key somewhere else.
    write(project, {**PROVIDER, "base_url": "https://collector.example.org/v1"})
    assert not project_confirmed(load_settings(project))
    write(project)
    assert project_confirmed(load_settings(project))  # Back to what was confirmed.


def test_a_comment_or_whitespace_change_asks_again(project):
    confirm_project(load_settings(project))
    project.write_bytes(project.read_bytes() + b"\n# a note\n")
    assert not project_confirmed(load_settings(project))


def test_confirmation_does_not_depend_on_how_the_file_is_named(repo, project, monkeypatch):
    spellings = [repo / "sub" / ".." / ".mosscode" / "config.yaml", Path(".mosscode") / "config.yaml", str(project)]
    (repo / "sub").mkdir()
    assert not any(project_confirmed(load_settings(path)) for path in spellings)
    confirm_project(load_settings(spellings[0]))
    assert all(project_confirmed(load_settings(path)) for path in spellings)


def test_another_project_with_the_same_content_is_not_confirmed(tmp_path, project):
    confirm_project(load_settings(project))
    other = tmp_path / "other" / ".mosscode" / "config.yaml"
    other.parent.mkdir(parents=True)
    other.write_bytes(project.read_bytes())
    assert not project_confirmed(load_settings(other))


def test_the_users_file_and_a_file_named_elsewhere_need_no_confirmation(tmp_path, isolated_home):
    user = write(isolated_home / ".mosscode" / "config.yaml")
    elsewhere = write(tmp_path / "configs" / "work.yaml")
    for path in (user, elsewhere):
        settings = load_settings(path)
        assert settings.project_digest == "" and project_confirmed(settings)


def test_the_record_is_kept_outside_the_project(repo, project, isolated_home):
    confirm_project(load_settings(project))
    records = list((isolated_home / ".mosscode" / "projects").rglob("config-trust.json"))
    assert len(records) == 1
    assert not any(path.name == "config-trust.json" for path in repo.rglob("*"))


# What the user is shown -----------------------------------------------------------------------------


def test_summary_names_each_host_and_credential_reference(project):
    write(
        project,
        PROVIDER,
        {"name": "vault", "protocol": "anthropic-messages", "model": "m", "api_key_keyring": "work-key"},
        {
            "name": "local",
            "protocol": "openai-chat-completions",
            "model": "m",
            "base_url": "http://127.0.0.1:8080/v1",
            "api_key": "sk-in-the-file",
        },
        web_search={"provider": "tavily", "api_key_env": "SEARCH_KEY"},
        shell="./tools/sh.exe",
    )
    text = project_summary(load_settings(project))
    assert "llm.example.net" in text and "环境变量 MOSS_TEST_KEY" in text
    assert "api.anthropic.com（官方地址）" in text and "凭据管理器中的 work-key" in text
    assert "127.0.0.1:8080（HTTP，未加密）" in text and "写在这份文件里的密钥" in text
    assert "sk-in-the-file" not in text
    assert "环境变量 SEARCH_KEY" in text
    assert "./tools/sh.exe" in text
    assert str(project) in text


def test_summary_leaves_out_the_shell_line_for_the_built_in_choices(project):
    write(project, shell="Bash")
    assert "命令通过这个程序执行" not in project_summary(load_settings(project))


def test_summary_cannot_be_rewritten_by_values_in_the_file(project):
    hidden = "ok\x1b[1A\x1b[2K\r\nfake line \u202egnp.exe"
    write(
        project,
        {**PROVIDER, "name": hidden, "model": hidden, "base_url": "https://\u0430pi.openai.com/v1"},  # A Cyrillic a.
        shell="sh\x1b[2K.exe",
    )
    text = project_summary(load_settings(project))
    assert not any(ch in text for ch in "\x1b\r\u202e")
    assert text.count("\n") == 5  # Two heading lines, three for the provider, one for the shell.
    assert "xn--" in text and "\u0430pi.openai.com" not in text


# Starting a session -----------------------------------------------------------------------------------


def start(monkeypatch, settings, *, prompt=None, config=None, trust=False, answer=None):
    asked = []

    class Panel:
        def __init__(self, otherwise):
            asked.append(otherwise)

        def run(self, inline, mouse):
            return answer

    monkeypatch.setattr(cli, "ConfigTrustApp", Panel)
    args = argparse.Namespace(prompt=prompt, config=config, trust_project_config=trust)
    return cli.confirm_config(args, settings), asked


def test_the_interface_asks_once_and_again_when_the_content_changes(project, monkeypatch, capsys):
    (used, notices), asked = start(monkeypatch, load_settings(project), answer=True)
    assert used.source == project and notices == () and asked == ["退出"]
    assert "llm.example.net" in capsys.readouterr().out
    assert start(monkeypatch, load_settings(project), answer=False)[1] == []  # Confirmed: not asked again.
    write(project, {**PROVIDER, "base_url": "https://collector.example.org/v1"})
    (used, _), asked = start(monkeypatch, load_settings(project), answer=None)
    assert asked == ["退出"] and used is None
    assert "collector.example.org" in capsys.readouterr().out
    assert not project_confirmed(load_settings(project))


@pytest.mark.parametrize("answer", [False, None])
def test_declining_falls_back_to_the_users_configuration(project, isolated_home, monkeypatch, answer):
    user = write(isolated_home / ".mosscode" / "config.yaml", {**PROVIDER, "name": "mine", "base_url": None})
    (used, notices), asked = start(monkeypatch, load_settings(project), answer=answer)
    assert used.source == user and [item.name for item in used.providers] == ["mine"]
    assert str(user) in asked[0]
    assert len(notices) == 1 and "下次启动时会再次询问" in notices[0]
    assert not project_confirmed(load_settings(project))


def test_declining_a_project_file_named_with_config_ends_the_run(project, isolated_home, monkeypatch, capsys):
    write(isolated_home / ".mosscode" / "config.yaml")
    (used, notices), asked = start(monkeypatch, load_settings(project), config=str(project), answer=False)
    assert used is None and notices == () and asked == ["退出"]
    assert "已退出" in capsys.readouterr().out


def test_headless_runs_refuse_an_unconfirmed_project_file(project, monkeypatch):
    with pytest.raises(ConfigError, match="--trust-project-config"):
        start(monkeypatch, load_settings(project), prompt="hi")
    (used, notices), asked = start(monkeypatch, load_settings(project), prompt="hi", trust=True)
    assert used.source == project and notices == () and asked == []
    assert not project_confirmed(load_settings(project))  # Trusted for this run only.
    confirm_project(load_settings(project))
    (used, _), asked = start(monkeypatch, load_settings(project), prompt="hi")
    assert used.source == project and asked == []


def test_a_record_that_cannot_be_written_trusts_this_run_only(project, monkeypatch):
    def refuse(settings, home=None):
        raise PermissionError(13, "拒绝访问")

    monkeypatch.setattr(cli, "confirm_project", refuse)
    (used, notices), _ = start(monkeypatch, load_settings(project), answer=True)
    assert used.source == project and len(notices) == 1 and "只在本次使用" in notices[0]


def test_a_headless_run_sends_nothing_before_the_project_file_is_confirmed(project, monkeypatch, capsys):
    monkeypatch.setenv("MOSS_TEST_KEY", "user-secret")
    monkeypatch.setattr(cli, "create_provider", lambda config: pytest.fail("a provider was created"))
    with pytest.raises(SystemExit) as stopped:
        cli.main(["-p", "hi"])
    assert stopped.value.code == 1
    assert "--trust-project-config" in capsys.readouterr().err


def test_listing_providers_needs_no_confirmation(project, capsys):
    cli.main(["--list-providers"])
    assert "gateway" in capsys.readouterr().out


# Team members ---------------------------------------------------------------------------------------


def test_the_lead_hands_members_the_content_it_is_using(tmp_path, project):
    digest = load_settings(project).project_digest
    arguments = member_arguments(tmp_path / "t", "alice", project, "gateway", digest)
    assert arguments[arguments.index("--team-config-digest") + 1] == digest
    assert "--team-config-digest" not in member_arguments(tmp_path / "t", "alice", project, "gateway")


def member(tmp_path, repo) -> TeamStore:
    store = TeamStore(tmp_path / "teams" / "t")
    store.create()
    store.acquire(os.getpid(), "", str(repo))
    store.modify(lambda team: team.members.append(Member("alice", "general", WINDOWS_TERMINAL, "test", "stopped")))
    return store


def test_a_member_takes_a_project_file_only_as_the_lead_was_using_it(tmp_path, repo, project, monkeypatch):
    from mosscode.teams import process

    def reached(*args, **options):
        raise RuntimeError("reached")

    monkeypatch.setattr(process, "member_conversation", reached)
    monkeypatch.setenv("MOSS_TEST_KEY", "user-secret")
    store = member(tmp_path, repo)

    def run(digest):
        assert process.run_member(str(store.directory), "alice", str(project), None, config_digest=digest) == 1
        return store.load().member("alice").error

    approved = load_settings(project).project_digest
    assert "未经确认" in run(None)  # Started by hand, or by a lead that never saw this file.
    assert "reached" in run(approved)
    # Changed after the lead started: the member must not pick the new content up.
    write(project, {**PROVIDER, "base_url": "https://collector.example.org/v1"})
    assert "未经确认" in run(approved)
    confirm_project(load_settings(project))
    assert "reached" in run(approved)  # The user has confirmed the new content since.


# Doctor ---------------------------------------------------------------------------------------------


def test_doctor_reports_a_project_file_until_it_is_confirmed(repo, project, isolated_home):
    from mosscode.doctor import WARN, check_config

    def warned():
        return [item.level for item in check_config(cwd=repo, home=isolated_home) if "尚未确认" in item.text]

    assert warned() == [WARN]
    confirm_project(load_settings(project))
    assert warned() == []
