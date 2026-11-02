"""Refinement 08 (docs/specs/refinement-08-cli-product/spec.md): the moss command, setup and doctor."""

import asyncio
from importlib.metadata import version
import io
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from mosscode import cli, doctor, launcher
from mosscode.config import ConfigError, ProviderConfig, Settings, find_config, load_settings, render_config
from mosscode.conversation import Conversation
from mosscode.setup import Wizard, run_setup, setup, user_config
from mosscode.tui import MossCodeApp
from test_cli import setup_cli
from test_conversation import FakeProvider


def modules_after(code: str) -> str:
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


# CL1, CL2, ST1, ST2 --------------------------------------------------------

@pytest.mark.parametrize("flag", ["--version", "-v"])
def test_version_prints_the_installed_version(capsys, flag):
    with pytest.raises(SystemExit) as exit:
        launcher.main([flag])
    assert exit.value.code == 0
    assert capsys.readouterr().out == f"mosscode {version('mosscode')}\n"


def test_help_lists_subcommands_and_examples(capsys):
    with pytest.raises(SystemExit):
        launcher.main(["--help"])
    out = capsys.readouterr().out
    for text in ("setup", "doctor", "moss -p", "moss -c", "PROMPT", "moss -- doctor"):
        assert text in out


def test_launcher_loads_no_interface_or_sdk():
    loaded = modules_after(
        "import sys\nfrom mosscode import launcher\n"
        "try:\n    launcher.main(['--version'])\nexcept SystemExit:\n    pass\n"
        "print([m for m in ('textual', 'anthropic', 'openai', 'mosscode.cli', 'mosscode.tui') if m in sys.modules])")
    assert loaded.splitlines()[-1] == "[]"


@pytest.mark.parametrize("protocol,loaded,absent", [
    ("openai-chat-completions", "openai", "anthropic"),
    ("openai-responses", "openai", "anthropic"),
    ("anthropic-messages", "anthropic", "openai"),
])
def test_a_provider_imports_only_its_sdk(protocol, loaded, absent):
    out = modules_after(
        "import sys\nfrom mosscode.config import ProviderConfig\nfrom mosscode.providers import create_provider\n"
        f"create_provider(ProviderConfig('p', {protocol!r}, 'm', api_key='k'))\n"
        f"print({loaded!r} in sys.modules, {absent!r} in sys.modules)")
    assert out == "True False"


# CL3, CL4, CL5 -------------------------------------------------------------

def test_arguments_positional_prompt_and_print_mode():
    args = launcher.parse_arguments(["解释入口"])
    assert args.initial == "解释入口" and args.prompt is None
    args = launcher.parse_arguments(["-p", "任务"])
    assert args.prompt == "任务" and args.initial is None
    args = launcher.parse_arguments(["任务", "-p"])  # An empty -p takes the positional task.
    assert args.prompt == "任务" and args.initial is None
    assert launcher.parse_arguments(["--", "doctor"]).initial == "doctor"


@pytest.mark.parametrize("argv", [
    ["-p", "a", "b"],
    ["hi", "--resume"],
    ["--output-format", "json", "hi"],
    ["-c", "--resume", "id"],
    ["--team-dir", "x"],
])
def test_argument_mistakes_exit_with_2(argv, capsys):
    with pytest.raises(SystemExit) as exit:
        launcher.parse_arguments(argv)
    assert exit.value.code == 2


class Terminal(io.StringIO):
    def isatty(self):
        return True


def test_piped_input_follows_the_task():
    assert launcher.with_stdin("解释", io.StringIO("Traceback\nline 2\n")) == "解释\n\nTraceback\nline 2"
    assert launcher.with_stdin("", io.StringIO("只有管道\n")) == "只有管道"
    assert launcher.with_stdin("任务", io.StringIO("\n  \n")) == "任务"
    assert launcher.with_stdin("任务", Terminal("不读取")) == "任务"


def test_main_appends_stdin_and_hands_over(monkeypatch):
    seen = []
    monkeypatch.setattr(sys, "stdin", io.StringIO("piped"))
    monkeypatch.setattr(cli, "run", seen.append)
    launcher.main(["-p", "任务"])
    assert seen[0].prompt == "任务\n\npiped"


def test_main_needs_a_task_and_a_terminal(monkeypatch, capsys):
    monkeypatch.setattr(cli, "run", lambda args: pytest.fail("must not start"))
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    with pytest.raises(SystemExit) as exit:
        launcher.main(["-p"])
    assert exit.value.code == 2 and "-p 需要任务" in capsys.readouterr().err
    monkeypatch.setattr(sys, "stdin", io.StringIO("not a terminal"))
    with pytest.raises(SystemExit) as exit:
        launcher.main([])
    assert exit.value.code == 2 and "交互界面需要终端" in capsys.readouterr().err


def test_subcommands_are_dispatched_before_parsing(capsys):
    with pytest.raises(SystemExit) as exit:
        launcher.main(["doctor", "--help"])
    assert exit.value.code == 0 and "moss doctor" in capsys.readouterr().out


def test_cli_passes_the_initial_prompt(monkeypatch):
    setup_cli(monkeypatch, ["列出文件"], multiple=False)
    seen = {}

    class Chat:
        def __init__(self, conversation, name, **kwargs):
            seen.update(kwargs)

        def run(self, **kwargs):
            pass

    monkeypatch.setattr(cli, "MossCodeApp", Chat)
    cli.main()
    assert seen["initial_prompt"] == "列出文件"


async def test_initial_prompt_is_sent_once_started():
    provider = FakeProvider()
    app = MossCodeApp(Conversation(provider), "test", initial_prompt="列出文件")
    async with app.run_test() as pilot:
        for _ in range(100):
            if app._reply_task is not None:
                break
            await pilot.pause(0.02)
        await asyncio.wait_for(app._reply_task, 5)
        assert provider.requests[0][-1].text == "列出文件"
        assert app._initial_prompt is None


# SU, CF --------------------------------------------------------------------

def wizard(answers, *, secret=None, environ=None):
    answers, out = iter(answers), []

    def read(question):
        out.append(question)
        answer = next(answers)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    return Wizard(read=read, secret=secret or (lambda question: pytest.fail("no secret asked")),
                  write=out.append, environ=environ or {}), out


def test_setup_writes_an_environment_variable_config(tmp_path):
    # Invalid answers are asked again: protocol 9, an ftp address, a window of "abc".
    w, out = wizard(["9", "1", "ftp://x", "", "claude-model", "abc", "200k", "", "", ""],
                    environ={"ANTHROPIC_API_KEY": "set"})
    assert setup(tmp_path, w) == "ready"
    settings = load_settings(user_config(tmp_path))
    provider = settings.providers[0]
    assert settings.default_provider == "claude"
    assert (provider.protocol, provider.model, provider.base_url) == ("anthropic-messages", "claude-model", None)
    assert provider.api_key_env == "ANTHROPIC_API_KEY" and provider.api_key is None
    assert provider.context_window == 200000
    assert any("请输入 1–3" in str(line) for line in out)


def test_setup_names_a_compatible_service_after_its_host(tmp_path):
    w, out = wizard(["3", "https://api.deepseek.com", "deepseek-chat", "", "", "", ""])
    assert setup(tmp_path, w) == "written"  # The variable is not set yet: say how, do not start.
    provider = load_settings(user_config(tmp_path)).providers[0]
    assert (provider.name, provider.api_key_env) == ("deepseek", "DEEPSEEK_API_KEY")
    assert any("SetEnvironmentVariable" in str(line) for line in out)
    asked = out.index("2/5 服务地址 base_url（回车使用官方地址 https://api.openai.com/v1）：")
    assert any("api.deepseek.com" in str(line) for line in out[:asked])  # The hint comes before the question.
    assert run_setup(tmp_path / "other", wizard(["3", "https://api.deepseek.com", "m", "", "", "", ""])[0]) is None


def test_setup_with_a_typed_key_never_shows_it(tmp_path):
    secret = "sk-test-do-not-show-123"
    w, out = wizard(["2", "", "gpt-model", "", "2", ""], secret=lambda question: secret)
    assert run_setup(tmp_path, w) == user_config(tmp_path)
    shown = "\n".join(map(str, out))
    assert secret not in shown and "明文" in shown and "已设置" in shown
    assert "服务地址 官方地址 https://api.openai.com/v1" in shown  # Where an empty answer goes is said.
    assert load_settings(user_config(tmp_path)).providers[0].credential() == secret


def test_setup_never_overwrites_and_writes_nothing_when_left(tmp_path):
    path = user_config(tmp_path)
    w, _ = wizard(["1", "", KeyboardInterrupt()])
    assert setup(tmp_path, w) == "cancelled" and not path.exists()
    w, _ = wizard(["1", "", "m", "", "", "", "n"])
    assert setup(tmp_path, w) == "cancelled" and not path.exists()
    path.parent.mkdir(parents=True)
    path.write_text("mine", encoding="utf-8")
    w, out = wizard([])
    assert setup(tmp_path, w) == "exists"
    assert path.read_text(encoding="utf-8") == "mine" and str(path) in out[0]


def test_setup_command_needs_a_terminal(monkeypatch, capsys):
    from mosscode import setup as module
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert module.main([]) == 2
    assert "终端" in capsys.readouterr().err


def test_render_config_is_valid_yaml_with_a_default(tmp_path):
    text = render_config({"name": "p", "protocol": "openai-responses", "model": "m", "api_key_env": "K"})
    assert text.startswith("# MossCode") and yaml.safe_load(text)["default_provider"] == "p"
    path = tmp_path / "c.yaml"
    path.write_text(text, encoding="utf-8")
    assert load_settings(path).providers[0].name == "p"


def test_first_run_starts_setup_only_in_a_terminal(monkeypatch):
    setup_cli(monkeypatch, [], multiple=False)

    def missing():
        raise ConfigError("找不到配置文件")

    monkeypatch.setattr(cli, "find_config", missing)
    monkeypatch.setattr(cli, "is_terminal", lambda stream: True)
    started = []
    monkeypatch.setattr("mosscode.setup.run_setup", lambda: started.append(1))
    monkeypatch.setattr(cli, "MossCodeApp", lambda *a, **k: pytest.fail("no configuration was written"))
    cli.main()
    assert started == [1]
    monkeypatch.setattr(cli, "is_terminal", lambda stream: False)
    with pytest.raises(SystemExit):
        cli.main()
    assert started == [1]


# DR ------------------------------------------------------------------------

def test_doctor_reports_credentials_without_values(tmp_path, monkeypatch):
    secret = "sk-doctor-secret-456"
    path = tmp_path / "c.yaml"
    path.write_text(yaml.safe_dump({"default_provider": "a", "providers": [
        {"name": "a", "protocol": "anthropic-messages", "model": "m", "api_key": secret,
         "base_url": "https://gateway.example/v1?token=x"},
        {"name": "b", "protocol": "openai-responses", "model": "m", "api_key_env": "MOSS_TEST_UNSET_KEY"},
    ]}), encoding="utf-8")
    monkeypatch.delenv("MOSS_TEST_UNSET_KEY", raising=False)
    items = doctor.check_config(str(path), cwd=tmp_path)
    text = doctor.report(items)
    assert secret not in text and "token=x" not in text
    assert "gateway.example" in text and "已设置" in text and "api.openai.com（官方地址）" in text
    assert [item.level for item in items] == [doctor.OK, doctor.OK, doctor.WARN]
    assert "MOSS_TEST_UNSET_KEY 未设置" in text


def test_doctor_missing_config_is_an_error_and_legacy_file_a_warning(tmp_path):
    cwd = tmp_path / "work"
    cwd.mkdir()
    (cwd / "config.yaml").write_text("providers: []", encoding="utf-8")
    items = doctor.check_config(cwd=cwd, home=tmp_path / "home")
    assert [item.level for item in items] == [doctor.ERROR, doctor.WARN]
    assert "moss setup" in items[0].fix and "--config" in items[1].fix


def test_home_folder_is_not_a_project(tmp_path):
    """Started in the home folder, ~/.mosscode is searched once and not checked as a project's files."""
    (tmp_path / ".mosscode").mkdir()
    with pytest.raises(ConfigError) as error:
        find_config(tmp_path, tmp_path)
    assert str(error.value).count("config.yaml") == 1
    assert doctor.check_project(tmp_path, home=tmp_path) == []
    project = tmp_path / "work"
    (project / ".mosscode").mkdir(parents=True)
    assert [item.level for item in doctor.check_project(project, home=tmp_path)] == [doctor.OK]


def test_doctor_exit_code(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(doctor, "check_home", lambda: [])
    assert doctor.main(["--config", str(tmp_path / "missing.yaml")]) == 1
    good = tmp_path / "good.yaml"
    good.write_text(render_config({"name": "p", "protocol": "openai-responses", "model": "m", "api_key": "k"}),
                    encoding="utf-8")
    assert doctor.main(["--config", str(good)]) == 0
    assert "✓ 配置" in capsys.readouterr().out


def test_doctor_path_lists_every_copy(tmp_path):
    first, second = tmp_path / "a", tmp_path / "b"
    suffix = ".exe" if os.name == "nt" else ""
    for folder in (first, second):
        folder.mkdir()
        for name in ("moss", "mosscode"):
            (folder / (name + suffix)).write_text("", encoding="utf-8")
    items = doctor.check_path(os.pathsep.join([str(first), str(second)]), scripts=first)
    assert [item.level for item in items] == [doctor.WARN, doctor.WARN] and str(second) in items[0].text
    items = doctor.check_path(str(first), scripts=first)
    assert [item.level for item in items] == [doctor.OK, doctor.OK]
    items = doctor.check_path(str(tmp_path / "none"), scripts=first)
    assert all(item.level == doctor.WARN and "update-shell" in item.fix for item in items)


def test_doctor_tools_install_method_and_home(tmp_path):
    items = doctor.check_tools(which=lambda name: None, windows=False)
    assert [item.level for item in items] == [doctor.WARN, doctor.WARN]
    assert doctor.install_method(str(Path("C:/Users/u/AppData/Roaming/uv/tools/mosscode"))).startswith("uv tool")
    assert [item.level for item in doctor.check_home(tmp_path)] == [doctor.OK]
    assert not list((tmp_path / ".mosscode").iterdir())  # The probe file is gone.
