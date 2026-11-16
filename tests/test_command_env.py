"""Commands the model runs get an environment without secrets and a private temp directory."""

import json
import os
import sys

import pytest
import yaml

from mosscode import command_env
from mosscode.command_env import command_environment, private_temp, removed
from mosscode.config import ConfigError, load_settings
from mosscode.credentials import protect
from mosscode.messages import ToolCall
from mosscode.tools import create_default_registry
from mosscode.tools.process import run_process

PRINT_ENV = "import json, os; print(json.dumps(dict(os.environ)))"


def config(tmp_path, **extra):
    path = tmp_path / "config.yaml"
    provider = {"name": "p", "protocol": "anthropic-messages", "model": "m", "api_key_env": "MOSS_TEST_PROVIDER"}
    path.write_text(yaml.safe_dump({"providers": [provider], **extra}), encoding="utf-8")
    return load_settings(path)


@pytest.mark.parametrize(
    "name, value",
    [
        ("GITHUB_TOKEN", "x"),
        ("NPM_AUTHTOKEN", "x"),
        ("AWS_SECRET_ACCESS_KEY", "x"),
        ("OPENAI_API_KEY", "x"),
        ("STRIPE_KEY", "x"),
        ("DB_PASSWORD", "x"),
        ("GPG_PASSPHRASE", "x"),
        ("AZURE_DEVOPS_PAT", "x"),
        ("SESSION_COOKIE", "x"),
        ("SOME_SETTING", "ghp_" + "a" * 36),
        ("SOME_SETTING", "sk-" + "b" * 40),
        ("SOME_SETTING", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJl"),
        ("SOME_SETTING", "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----"),
        ("DATABASE_URL", "postgres://user:hunter2@db.example.com/app"),
    ],
)
def test_secrets_are_recognized_by_name_or_value(name, value):
    assert removed(name, value)


@pytest.mark.parametrize(
    "name, value",
    [
        ("PATH", "/usr/bin"),
        ("TOKENIZERS_PARALLELISM", "false"),
        ("XAUTHORITY", "/run/user/1000/xauth"),
        ("HTTPS_PROXY", "http://user:pass@127.0.0.1:7897"),
        ("DATABASE_URL", "postgres://db.example.com/app"),
        ("MOSSCODE_COMMAND", "echo ghp_" + "a" * 36),  # MossCode's own variables carry the command itself.
        ("KEYBOARD_LAYOUT", "us"),
    ],
)
def test_ordinary_variables_stay(name, value):
    assert not removed(name, value)


def test_keep_and_drop_from_config(tmp_path):
    protect(config(tmp_path, command_env={"keep": ["GITHUB_*"], "drop": ["INTERNAL_URL"]}))
    assert not removed("GITHUB_TOKEN", "x") and removed("GH_TOKEN", "x")
    assert removed("INTERNAL_URL", "https://example.com")
    for bad in ({"keep": "GITHUB_TOKEN"}, {"other": []}, {"drop": ["A B"]}, ["x"]):
        with pytest.raises(ConfigError, match="command_env"):
            config(tmp_path, command_env=bad)


async def test_commands_and_skill_tools_lose_secrets_but_hooks_keep_them(tmp_path, monkeypatch):
    monkeypatch.setenv("MOSS_TEST_TOKEN", "t-1")
    monkeypatch.setenv("MOSS_TEST_PLAIN", "plain")
    script = [sys.executable, "-c", PRINT_ENV]
    confined = json.loads((await run_process(script, tmp_path, confined=True))["stdout"])
    assert "MOSS_TEST_TOKEN" not in confined and confined["MOSS_TEST_PLAIN"] == "plain"
    hook = json.loads((await run_process(script, tmp_path))["stdout"])
    assert hook["MOSS_TEST_TOKEN"] == "t-1"  # Hooks are the user's own commands.


async def test_execute_command_runs_confined(tmp_path, monkeypatch):
    monkeypatch.setenv("MOSS_TEST_TOKEN", "t-1")
    registry = create_default_registry(tmp_path, permission_mode="bypassPermissions", load_permissions=False)
    command = f"\"{sys.executable}\" -c \"import os; print(os.environ.get('MOSS_TEST_TOKEN', 'absent'))\""
    if registry.get("execute_command").shell.kind == "powershell":
        command = "& " + command
    result = await registry.execute(ToolCall("c", "execute_command", json.dumps({"command": command})))
    assert not result.is_error and result.output.strip() == "absent"


async def test_no_session_or_control_variables_reach_a_command(tmp_path):
    env = json.loads((await run_process([sys.executable, "-c", PRINT_ENV], tmp_path, confined=True))["stdout"])
    assert not {"MOSSCODE_SESSION_ID", "MOSSCODE_HOOK_ID", "MOSSCODE_AGENT"} & set(env)


async def test_private_temp_directory(tmp_path, isolated_home):
    script = [sys.executable, "-c", "import tempfile; print(tempfile.gettempdir())"]
    first = (await run_process(script, tmp_path, confined=True))["stdout"].strip()
    temp = private_temp()
    assert os.path.samefile(first, temp) and temp.parent == isolated_home / ".mosscode" / "tmp"
    assert temp.name.startswith(f"{os.getpid()}-")
    env = command_environment({"TEMP": "C:/elsewhere", "tmp": "C:/elsewhere"})
    assert {name for name in env if name.upper() in {"TEMP", "TMP", "TMPDIR"}} == {"TEMP", "TMP", "TMPDIR"}
    command_env.reset()  # Another process gets another directory.
    assert private_temp() != temp


def test_stale_temp_directories_are_swept(isolated_home):
    parent = isolated_home / ".mosscode" / "tmp"
    stale, fresh = parent / "1-old", parent / "2-new"
    stale.mkdir(parents=True)
    fresh.mkdir()
    os.utime(stale, (0, 0))
    private_temp()
    assert not stale.exists() and fresh.exists()


CLIPBOARD_READ = """
import ctypes
from ctypes import wintypes
user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.GetClipboardData.restype = wintypes.HANDLE
user32.OpenClipboard(None)
available = user32.IsClipboardFormatAvailable(13)  # CF_UNICODETEXT
data = user32.GetClipboardData(13)
error = ctypes.get_last_error()
user32.CloseClipboard()
print(available, bool(data), error)
"""


@pytest.mark.skipif(sys.platform != "win32", reason="Job Object UI limits are Windows only")
async def test_commands_cannot_read_the_clipboard_on_windows(tmp_path):
    # Reads only: the test never changes what is on the clipboard.
    hook = (await run_process([sys.executable, "-c", CLIPBOARD_READ], tmp_path))["stdout"].split()
    if hook != ["1", "True", "0"]:
        pytest.skip("no text on the clipboard to compare against")
    confined = (await run_process([sys.executable, "-c", CLIPBOARD_READ], tmp_path, confined=True))["stdout"].split()
    assert confined[1:] == ["False", "5"]  # ERROR_ACCESS_DENIED
