"""API keys stay out of child processes, tool output and the history; they can live in the credential manager."""

import io
import json
import sys

import keyring
import pytest
import yaml
from keyring.backends import fail

from mosscode import credentials, doctor
from mosscode.config import ConfigError, load_settings
from mosscode.conversation import Conversation
from mosscode.credentials import PLACEHOLDER, child_environment, plaintext_notice, protect, redact
from mosscode.key_command import main as key_main
from mosscode.messages import ToolCall
from mosscode.setup import setup, user_config
from mosscode.tools import create_default_registry
from mosscode.tools.process import run_process
from test_cli_product import wizard
from test_conversation import FakeProvider

KEY = "moss-test-secret-0123456789"  # Not shaped like a real provider key, so secret scanners leave it alone.


def config_file(tmp_path, provider=None, **extra):
    provider = {"name": "deepseek", "protocol": "anthropic-messages", "model": "m", **(provider or {})}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"providers": [provider], **extra}), encoding="utf-8")
    return path


def no_backend(monkeypatch):
    backend = fail.Keyring()
    monkeypatch.setattr(keyring, "get_keyring", lambda: backend)
    for action in ("get_password", "set_password", "delete_password"):
        monkeypatch.setattr(keyring, action, getattr(backend, action))


async def test_child_processes_do_not_inherit_the_variables_holding_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("MOSS_TEST_KEY", KEY)
    monkeypatch.setenv("MOSS_TEST_OTHER", "kept")
    script = "import os; print(os.environ.get('MOSS_TEST_KEY', 'absent'), os.environ.get('MOSS_TEST_OTHER'))"
    assert child_environment(None) is None  # Nothing registered: inherit as before.
    protect(load_settings(config_file(tmp_path, {"api_key_env": "MOSS_TEST_KEY"})))
    for env in (None, {"MOSS_TEST_KEY": KEY, "MOSS_TEST_OTHER": "kept", **dict(child_environment(None))}):
        details = await run_process([sys.executable, "-c", script], tmp_path, check=False, env=env)
        assert details["stdout"].split() == ["absent", "kept"]
    if sys.platform == "win32":  # Windows variable names ignore case.
        assert "moss_test_key" not in child_environment({"moss_test_key": KEY})


async def test_a_key_in_tool_output_is_hidden_before_the_model_or_the_transcript_sees_it(tmp_path, monkeypatch):
    path = config_file(
        tmp_path, {"api_key": KEY, "headers": {"X-Api-Key": "header-secret-1", "X-Version": "v1-public"}}
    )
    protect(load_settings(path))
    registry = create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False)
    result = await registry.execute(ToolCall("r", "read_file", json.dumps({"path": "config.yaml"})))
    assert KEY not in result.output and "header-secret-1" not in result.output and PLACEHOLDER in result.output
    assert "v1-public" in result.output  # Headers that are not credentials stay.
    # Writing the placeholder back would overwrite the real key.
    written = await registry.execute(
        ToolCall("w", "write_file", json.dumps({"path": "config.yaml", "content": result.output}))
    )
    assert written.is_error and written.code == "secret_placeholder" and KEY in path.read_text(encoding="utf-8")
    edit = {"path": "config.yaml", "old_text": "deepseek", "new_text": f"name {PLACEHOLDER}"}
    assert (await registry.execute(ToolCall("e", "edit_file", json.dumps(edit)))).code == "secret_placeholder"
    assert redact("short") == "short" and redact(f"a{KEY}b") == f"a{PLACEHOLDER}b"
    credentials.remember("abc")  # Too short to hide without hiding ordinary text.
    assert redact("abc") == "abc"


async def test_messages_joining_the_history_are_hidden_too(tmp_path):
    protect(load_settings(config_file(tmp_path, {"api_key": KEY})))
    provider = FakeProvider()
    conv = Conversation(provider)
    await conv.ask(f"我的密钥是 {KEY}，帮我看看")
    sent = provider.requests[0][0]
    assert KEY not in sent.text and PLACEHOLDER in sent.text
    assert KEY not in conv.history[0].text


def test_keys_in_the_credential_manager(tmp_path, memory_keyring, monkeypatch):
    path = config_file(
        tmp_path, {"api_key_keyring": "deepseek"}, web_search={"provider": "tavily", "api_key_keyring": "tavily"}
    )
    settings = load_settings(path)
    with pytest.raises(ConfigError, match="moss key set deepseek"):
        settings.providers[0].credential()
    assert settings.web_search.credential() is None
    keyring.set_password("mosscode", "deepseek", KEY)
    assert settings.providers[0].credential() == KEY and redact(KEY) == PLACEHOLDER
    with pytest.raises(ConfigError, match="只选择一种"):
        load_settings(config_file(tmp_path, {"api_key_keyring": "a", "api_key": "b"}))
    no_backend(monkeypatch)
    with pytest.raises(ConfigError, match="没有可用的系统凭据管理器"):
        settings.providers[0].credential()


def test_protect_reads_credential_manager_keys_up_front(tmp_path):
    keyring.set_password("mosscode", "other", "kr-other-secret-42")
    path = config_file(tmp_path, {"api_key_keyring": "other"})
    protect(load_settings(path))
    assert redact("kr-other-secret-42") == PLACEHOLDER


def test_moss_key_saves_and_deletes_without_showing_the_key(memory_keyring, capsys, monkeypatch):
    assert key_main(["set", "deepseek"], stdin=io.StringIO(KEY + "\n")) == 0
    assert memory_keyring.entries[("mosscode", "deepseek")] == KEY
    out = capsys.readouterr()
    assert KEY not in out.out + out.err and "api_key_keyring: deepseek" in out.out
    assert key_main(["set", "deepseek"], stdin=io.StringIO("\n")) == 1  # Empty: nothing saved over it.
    assert memory_keyring.entries[("mosscode", "deepseek")] == KEY
    assert key_main(["delete", "deepseek"]) == 0 and not memory_keyring.entries
    assert key_main(["delete", "deepseek"]) == 1
    no_backend(monkeypatch)
    capsys.readouterr()
    assert key_main(["set", "x"], stdin=io.StringIO(KEY)) == 1
    assert "没有可用的系统凭据管理器" in capsys.readouterr().err


class Vault:
    def __init__(self, problem=None):
        self.saved, self._problem = {}, problem

    def problem(self):
        return self._problem

    def label(self):
        return "Windows 凭据管理器"

    def store(self, name, key):
        self.saved[name] = key


def test_setup_recommends_the_credential_manager_when_there_is_one(tmp_path):
    vault = Vault()
    w, out = wizard(["8", "3", "https://api.deepseek.com", "deepseek-chat", "", "", ""], secret=lambda question: KEY)
    w.vault = vault
    assert setup(tmp_path, w) == "ready"
    provider = load_settings(user_config(tmp_path)).providers[0]
    assert provider.api_key_keyring == "deepseek" and provider.api_key is None
    assert vault.saved == {"deepseek": KEY}
    shown = "\n".join(map(str, out))
    assert KEY not in shown and "1. 系统凭据管理器（推荐）" in shown and "凭据 系统凭据管理器 deepseek" in shown
    assert KEY not in user_config(tmp_path).read_text(encoding="utf-8")
    # Left before confirming: nothing saved.
    vault = Vault()
    w, _ = wizard(["8", "3", "https://api.deepseek.com", "m", "", "", "n"], secret=lambda question: KEY)
    w.vault = vault
    assert setup(tmp_path / "other", w) == "cancelled" and vault.saved == {}


def test_setup_without_a_credential_manager_says_why_and_offers_the_rest(tmp_path):
    w, out = wizard(["8", "3", "https://api.deepseek.com", "m", "", "", "", ""])
    w.vault = Vault("这台机器没有可用的系统凭据管理器")
    setup(tmp_path, w)
    shown = "\n".join(map(str, out))
    assert "不能使用系统凭据管理器" in shown and "1. 环境变量（推荐）" in shown
    assert load_settings(user_config(tmp_path)).providers[0].api_key_env == "DEEPSEEK_API_KEY"


def test_plaintext_key_inside_the_project_is_pointed_out(tmp_path):
    settings = load_settings(config_file(tmp_path, {"api_key": KEY}))
    notice = plaintext_notice(settings, tmp_path)
    assert "moss key set deepseek" in notice and KEY not in notice
    assert plaintext_notice(settings, tmp_path / "elsewhere") is None
    assert plaintext_notice(load_settings(config_file(tmp_path, {"api_key_keyring": "d"})), tmp_path) is None


def test_doctor_reports_credential_manager_entries(tmp_path):
    path = config_file(tmp_path, {"api_key_keyring": "deepseek"})
    item = next(i for i in doctor.check_config(str(path), cwd=tmp_path) if "Provider" in i.text)
    assert item.level == doctor.WARN and "系统凭据管理器 deepseek 未找到" in item.text
    assert item.fix == "运行 moss key set deepseek"
    keyring.set_password("mosscode", "deepseek", KEY)
    items = doctor.check_config(str(path), cwd=tmp_path)
    item = next(i for i in items if "Provider" in i.text)
    assert item.level == doctor.OK and "系统凭据管理器 deepseek 已设置" in item.text
    assert KEY not in doctor.report(items)
