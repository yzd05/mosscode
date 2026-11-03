import json

import httpx
import pytest
import yaml

from mosscode import launcher, models_command
from mosscode.catalog import encode, load_catalog, save, slim, user_path


def raw(*names, window=1_000_000):
    return {
        name: {
            "mode": "chat",
            "litellm_provider": name.split("/")[0] if "/" in name else "deepseek",
            "max_input_tokens": window,
            "max_output_tokens": 393_216,
            "input_cost_per_token": 3e-7,
            "output_cost_per_token": 1.2e-6,
            "supports_vision": True,
        }
        for name in names
    }


def test_download_follows_redirects_and_reports_http_errors():
    def handler(request):
        if request.url.path == "/old":
            return httpx.Response(302, headers={"location": "https://example.test/new"})
        if request.url.path == "/new":
            return httpx.Response(200, content=b"{}")
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    assert models_command.download("https://example.test/old", transport=transport) == b"{}"
    with pytest.raises(httpx.HTTPStatusError):
        models_command.download("https://example.test/missing", transport=transport)


def test_update_replaces_the_user_copy_and_counts_the_changes(tmp_path):
    save(encode(slim(raw("deepseek/a", "deepseek/b"), "2026-01-01")), tmp_path)
    fresh = raw("deepseek/b", "deepseek/c") | {"deepseek/b": raw("deepseek/b", window=64_000)["deepseek/b"]}
    text, code = models_command.update(tmp_path, fetch=lambda: json.dumps(fresh).encode())
    assert code == 0 and "2 个模型（新增 1，删除 1，变化 1）" in text and str(user_path(tmp_path)) in text
    assert set(load_catalog(tmp_path).models) == {"deepseek/b", "deepseek/c"}


@pytest.mark.parametrize(
    "fetch,reason",
    [
        (lambda: (_ for _ in ()).throw(httpx.ConnectError("offline")), "下载失败：offline"),
        (lambda: b"<html>", "无法解析"),
        (lambda: b"[]", "无法解析"),
        (lambda: json.dumps({"x": {"mode": "embedding", "litellm_provider": "openai"}}).encode(), "没有聊天模型"),
    ],
)
def test_a_failed_update_keeps_the_old_copy(tmp_path, fetch, reason):
    path = save(encode(slim(raw("deepseek/a"), "2026-01-01")), tmp_path)
    before = path.read_bytes()
    text, code = models_command.update(tmp_path, fetch=fetch)
    assert code == 1 and reason in text and "原有目录没有改动" in text
    assert path.read_bytes() == before and list(path.parent.glob("*.tmp")) == []


def test_search_lists_matching_entries_with_prices_and_a_limit():
    save(encode(slim(raw(*(f"moonshot/kimi-{n:02}" for n in range(35)), "deepseek/deepseek-v4-flash"), "2026-01-02")))
    catalog = load_catalog()
    text = models_command.search("v4-FLASH", catalog)
    row = next(line for line in text.splitlines() if line.startswith("deepseek/deepseek-v4-flash"))
    assert row.split() == ["deepseek/deepseek-v4-flash", "deepseek", "1M", "384K", "$0.3", "$1.2", "是"]
    assert "catalog_model" in text
    many = models_command.search("kimi", catalog)
    assert sum(line.startswith("moonshot/kimi-") for line in many.splitlines()) == 30 and "另有 5 个" in many
    assert models_command.search("nothing", catalog) == "目录中没有名字包含 nothing 的模型。"


def test_without_a_keyword_each_provider_shows_its_match(tmp_path):
    save(encode(slim(raw("deepseek/deepseek-v4-flash"), "2026-01-02")))
    base = {"protocol": "anthropic-messages", "base_url": "https://api.deepseek.com/anthropic", "api_key_env": "K"}
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "providers": [
                    {"name": "a", "model": "deepseek-v4-flash", **base},
                    {"name": "b", "model": "flash", **base},
                    {"name": "c", "model": "fast", "catalog_model": "deepseek/deepseek-v4-flash", **base},
                ]
            }
        ),
        encoding="utf-8",
    )
    text, code = models_command.matches(str(path))
    assert code == 0
    assert "  a：deepseek-v4-flash → deepseek/deepseek-v4-flash（按模型名，窗口 1M，输出上限 32K）" in text
    assert "  b：flash → 目录中没有，按 128K 估算上下文" in text
    assert "  c：fast → deepseek/deepseek-v4-flash（catalog_model，窗口 1M，输出上限 32K）" in text


def test_the_subcommand_runs_from_the_launcher(capsys):
    save(encode(slim(raw("deepseek/deepseek-v4-flash"), "2026-01-02")))
    with pytest.raises(SystemExit) as exit:
        launcher.main(["models", "v4"])
    out = capsys.readouterr().out
    assert exit.value.code == 0 and "deepseek/deepseek-v4-flash" in out
    assert "模型目录：1 个模型，数据日期 2026-01-02" in out and "LiteLLM" in out
    with pytest.raises(SystemExit) as exit:
        launcher.main(["models", "--config", "missing.yaml"])
    assert exit.value.code == 1 and "无法读取配置" in capsys.readouterr().out


def test_search_lists_the_makers_own_services_before_resellers():
    save(encode(slim(raw("aihubmix/kimi-a", "bedrock/us-east-1/kimi-b", "moonshot/kimi-z"), "2026-01-02")))
    lines = models_command.search("kimi", load_catalog()).splitlines()[1:4]
    assert [line.split()[0] for line in lines] == ["moonshot/kimi-z", "aihubmix/kimi-a", "bedrock/us-east-1/kimi-b"]
