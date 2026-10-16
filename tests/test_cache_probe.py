import json

from mosscode.config import ProviderConfig, Settings
from mosscode.prompts import build_system_prompt
from scripts import probe_prompt_cache as probe_script
from test_agent import SequenceProvider, call, response


async def test_probe_makes_three_explicit_requests_and_never_executes_tools(tmp_path, monkeypatch, capsys):
    target = tmp_path / "must-not-exist.txt"
    dangerous = response("private-reply", [call("write_file", path=str(target), content="private-test-key")])
    provider = SequenceProvider(dangerous, dangerous, dangerous)
    settings = Settings((ProviderConfig("test", "openai-chat-completions", "mock"),))
    monkeypatch.setattr(probe_script, "load_settings", lambda path: settings)
    monkeypatch.setattr(probe_script, "create_provider", lambda config: provider)
    records = await probe_script.probe("explicit-test-config", "test")
    assert len(provider.requests) == 3 and len(records) == 3 and not target.exists()
    assert all(r[3] is False for r in provider.requests)
    assert provider.requests[0] == provider.requests[1]
    assert provider.requests[0][1].stable == build_system_prompt(provider.requests[0][2], override=settings.system_prompt)
    assert provider.requests[2][1].stable == provider.requests[0][1].stable
    assert provider.requests[2][1].supplements[-1].kind == "probe-context"
    assert provider.closed == 3  # SequenceProvider records closed streams.
    output = capsys.readouterr().out
    assert "private-test-key" not in output and "private-reply" not in output and "<environment>" not in output
    assert all(json.loads(line)["cache_observation"] == "unreported" for line in output.splitlines())
