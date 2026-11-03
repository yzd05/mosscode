import sys
from pathlib import Path

import pytest

from mosscode import cli
from mosscode.config import ProviderConfig, Settings
from test_conversation import FakeProvider


def setup_cli(monkeypatch, args, *, multiple=True, selection="second", default="first"):
    from mosscode.mcp_config import MCPSettings

    monkeypatch.setattr(cli, "load_mcp_settings", lambda root: MCPSettings())
    configs = (
        ProviderConfig("first", "anthropic-messages", "model-a"),
        ProviderConfig("second", "openai-chat-completions", "model-b"),
    )
    settings = Settings(configs if multiple else configs[:1], default)
    provider = FakeProvider()
    calls = {"selection": 0, "created": [], "chat": []}

    class Picker:
        def __init__(self, loaded):
            assert loaded is settings

        def run(self, *, inline, mouse):
            assert inline is True
            assert mouse is False
            calls["selection"] += 1
            return None if selection is None else settings.select(selection)

    class Chat:
        def __init__(self, conversation, name, **kwargs):
            calls["chat"].append(name)

        def run(self, *, inline, inline_no_clear, mouse):
            assert inline is True
            assert inline_no_clear is True
            assert mouse is False

    def create(config):
        calls["created"].append(config.name)
        return provider

    monkeypatch.setattr(sys, "argv", ["mosscode", *args])
    # Never look for a real config.yaml: it is gitignored, so a fresh clone has none.
    monkeypatch.setattr(cli, "find_config", lambda: Path("config.yaml"))
    monkeypatch.setattr(cli, "load_settings", lambda path: settings)
    monkeypatch.setattr(cli, "ProviderSelectApp", Picker)
    monkeypatch.setattr(cli, "MossCodeApp", Chat)
    monkeypatch.setattr(cli, "create_provider", create)
    return provider, calls


@pytest.mark.parametrize(
    "multiple,args,default,expected,pickers",
    [
        (False, [], None, "first", 0),
        (True, [], None, "second", 1),
        (True, [], "second", "second", 0),
        (True, ["--provider", "first"], "second", "first", 0),
        (True, ["--provider", "first"], None, "first", 0),
    ],
)
def test_interactive_startup_uses_selected_configuration(monkeypatch, multiple, args, default, expected, pickers):
    _, calls = setup_cli(monkeypatch, args, multiple=multiple, default=default)
    cli.main()
    assert calls["selection"] == pickers
    assert calls["created"] == [expected]
    assert calls["chat"][0] == expected


def test_cancelled_selection_creates_no_client_or_conversation(monkeypatch):
    _, calls = setup_cli(monkeypatch, [], selection=None, default=None)
    cli.main()
    assert calls["selection"] == 1
    assert calls["created"] == []
    assert calls["chat"] == []


@pytest.mark.parametrize("default", ["first", None])
def test_headless_prompt_uses_default_and_closes_provider(monkeypatch, capsys, default):
    provider, calls = setup_cli(monkeypatch, ["--prompt", "hello"], default=default)
    cli.main()
    assert calls["selection"] == 0
    assert calls["chat"] == []
    assert calls["created"] == ["first"]
    assert capsys.readouterr().out == "partial\n"
    assert provider.closed


def test_headless_usage_is_separate_from_response(monkeypatch, capsys):
    provider, _ = setup_cli(monkeypatch, ["--prompt", "hello", "--show-usage"])
    cli.main()
    captured = capsys.readouterr()
    assert captured.out == "partial\n"
    assert "缓存读 未提供 / 写 未提供" in captured.err
    assert "<environment>" not in captured.out + captured.err
    assert provider.closed


def test_listing_does_not_open_selector_or_client(monkeypatch, capsys):
    _, calls = setup_cli(monkeypatch, ["--list-providers"])
    cli.main()
    assert "second: openai-chat-completions / model-b" in capsys.readouterr().out
    assert calls == {"selection": 0, "created": [], "chat": []}


def test_unknown_explicit_provider_fails_without_opening_selector(monkeypatch, capsys):
    _, calls = setup_cli(monkeypatch, ["--provider", "missing"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    assert "未知 Provider：missing" in capsys.readouterr().err
    assert calls == {"selection": 0, "created": [], "chat": []}
