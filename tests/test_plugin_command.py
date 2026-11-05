"""`moss plugin`: sources, marketplaces, confirmation, update, remove and the doctor section; git only on local repos."""

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from mosscode import doctor
from mosscode.plugin_command import main, report
from mosscode.plugins import PluginRecord, data_dir, installed, load_records, plugins_home, repo_dir, save_records
from test_plugins import install_record, make_plugin


class Terminal(io.StringIO):
    def isatty(self):
        return True


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def repository(path: Path, name: str = "kit") -> str:
    """A git repository holding one plugin; returns its file:// address."""
    make_plugin(path, manifest={"name": name, "version": "1.0.0", "description": f"{name} 工具"})
    git(path, "init", "--quiet", "--initial-branch", "main")
    git(path, "add", "-A")
    git(path, "commit", "--quiet", "-m", "first")
    return path.as_uri()


def commit_command(path: Path, name: str) -> None:
    (path / "commands" / f"{name}.md").write_text(f"# {name}\n做 {name}\n", encoding="utf-8")
    git(path, "add", "-A")
    git(path, "commit", "--quiet", "-m", name)


def leftovers(home: Path) -> list[Path]:
    tmp = plugins_home(home) / ".tmp"
    return list(tmp.iterdir()) if tmp.is_dir() else []


def head_of(path: Path) -> str:
    return git(path, "rev-parse", "HEAD")


@pytest.fixture
def home(tmp_path):
    return tmp_path / "home"


# Install ------------------------------------------------------------------------------------


def test_install_from_a_local_directory_uses_it_in_place(tmp_path, home, capsys):
    source = make_plugin(tmp_path / "src", manifest={"name": "kit"})
    assert main(["install", str(source), "-y"], home=home) == 0
    out = capsys.readouterr().out
    for text in ("插件 kit", "lint", "ship", "checker", "kit-server", "以你的权限运行", "已安装插件 kit"):
        assert text in out
    [record] = load_records(home)[0]
    assert (record.name, record.source, record.kind) == ("kit", source.resolve().as_posix(), "local")
    assert not (plugins_home(home) / "repos").exists()
    assert installed(home).plugins[0].root == source.resolve()


def test_install_from_git_clones_at_a_tag_and_previews_final_paths(tmp_path, home, capsys):
    url = repository(tmp_path / "repo")
    commit_command(tmp_path / "repo", "bad_name")
    git(tmp_path / "repo", "tag", "v1")
    assert main(["install", url, "--ref", "v1", "-y"], home=home) == 0
    out = capsys.readouterr().out
    assert repo_dir("kit", home).as_posix() in out  # The server path as it will be, not the temporary clone.
    assert str(repo_dir("kit", home) / "commands" / "bad_name.md") in out  # So is a problem's file.
    assert ".tmp" not in out
    [record] = load_records(home)[0]
    assert (record.source, record.ref, record.kind) == (url, "v1", "git")
    assert (repo_dir("kit", home) / ".claude-plugin" / "plugin.json").is_file()
    assert not data_dir("kit", home).exists()  # Showing it wrote nothing.
    assert leftovers(home) == []


def test_install_without_a_terminal_needs_yes_and_leaves_nothing(tmp_path, home, capsys):
    url = repository(tmp_path / "repo")
    assert main(["install", url], home=home, stdin=io.StringIO("y\n")) == 1
    captured = capsys.readouterr()
    assert "插件 kit" in captured.out and "已取消" in captured.out
    assert "-y" in captured.err
    assert load_records(home)[0] == []
    assert not repo_dir("kit", home).exists()
    assert leftovers(home) == []


@pytest.mark.parametrize("answer,status,count", [("y\n", 0, 1), ("n\n", 1, 0), ("\n", 1, 0)])
def test_install_asks_on_a_terminal(tmp_path, home, answer, status, count):
    url = repository(tmp_path / "repo")
    assert main(["install", url], home=home, stdin=Terminal(answer)) == status
    assert len(load_records(home)[0]) == count
    assert leftovers(home) == []


def test_install_refuses_bad_names_duplicates_and_a_ref_on_a_local_directory(tmp_path, home, capsys):
    source = make_plugin(tmp_path / "src")
    assert main(["install", str(source), "--name", "Bad Name", "-y"], home=home) == 1
    assert "--name" in capsys.readouterr().err
    assert main(["install", str(source), "--ref", "main", "-y"], home=home) == 1
    assert "file://" in capsys.readouterr().err
    assert main(["install", str(source), "--name", "kit", "-y"], home=home) == 0
    assert main(["install", str(source), "--name", "kit", "-y"], home=home) == 1
    assert "已安装名为 kit" in capsys.readouterr().err
    assert main(["install", str(tmp_path / "nowhere"), "-y"], home=home) == 1
    assert "不是本机目录" in capsys.readouterr().err
    empty = tmp_path / "empty"
    empty.mkdir()
    assert main(["install", str(empty), "-y"], home=home) == 1
    assert "不是插件" in capsys.readouterr().err
    assert [record.name for record in load_records(home)[0]] == ["kit"]


def test_a_marketplace_lists_its_plugins_and_installs_one_by_name(tmp_path, home, capsys):
    other = repository(tmp_path / "other", "other")
    market = tmp_path / "market"
    make_plugin(market / "plugins" / "kit", manifest={"name": "kit"})
    (market / ".claude-plugin").mkdir()
    (market / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps(
            {
                "name": "market",
                "owner": {"name": "t"},
                "plugins": [
                    {"name": "kit", "source": "./plugins/kit", "description": "本仓库中的插件"},
                    {"name": "other", "source": {"source": "url", "url": other}, "description": "别处的插件"},
                    {"name": "far", "source": {"source": "npm", "package": "x"}},
                    {"name": "escape", "source": "../outside"},
                ],
            }
        ),
        encoding="utf-8",
    )
    git(market, "init", "--quiet", "--initial-branch", "main")
    git(market, "add", "-A")
    git(market, "commit", "--quiet", "-m", "market")
    url = market.as_uri()

    assert main(["install", url], home=home) == 1
    out = capsys.readouterr().out
    assert "marketplace" in out and "本仓库中的插件" in out and "别处的插件" in out
    assert leftovers(home) == []

    assert main(["install", f"{url}#kit", "-y"], home=home) == 0
    assert main(["install", f"{url}#other", "-y"], home=home) == 0
    kit, second = load_records(home)[0]
    assert (kit.source, kit.path) == (url, "plugins/kit")
    assert (repo_dir("kit", home) / "plugins" / "kit" / "skills" / "lint" / "SKILL.md").is_file()
    assert [plugin.name for plugin in installed(home).plugins] == ["kit", "other"]
    assert (second.source, second.path) == (other, None)
    capsys.readouterr()

    assert main(["install", f"{url}#far", "-y"], home=home) == 1
    assert "不受支持" in capsys.readouterr().err
    assert main(["install", f"{url}#escape", "-y"], home=home) == 1
    assert "不在仓库内" in capsys.readouterr().err
    assert main(["install", f"{url}#missing", "-y"], home=home) == 1
    assert "没有名为 missing" in capsys.readouterr().err
    assert leftovers(home) == []


# Update -------------------------------------------------------------------------------------


def test_update_follows_a_branch_and_shows_what_changed(tmp_path, home, capsys):
    source = tmp_path / "repo"
    url = repository(source)
    assert main(["install", url, "-y"], home=home) == 0
    commit_command(source, "deploy")
    capsys.readouterr()
    assert main(["update", "-y"], home=home) == 0
    out = capsys.readouterr().out
    assert "+ 命令 /deploy" in out and "已更新插件 kit" in out
    assert head_of(repo_dir("kit", home)) == head_of(source)
    assert main(["update", "kit", "-y"], home=home) == 0
    assert "已是最新" in capsys.readouterr().out
    assert leftovers(home) == []


def test_update_leaves_a_pinned_tag_where_it_is(tmp_path, home, capsys):
    source = tmp_path / "repo"
    url = repository(source)
    git(source, "tag", "v1")
    assert main(["install", url, "--ref", "v1", "-y"], home=home) == 0
    pinned = head_of(repo_dir("kit", home))
    commit_command(source, "deploy")
    assert main(["update", "-y"], home=home) == 0
    assert "固定在 v1" in capsys.readouterr().out
    assert head_of(repo_dir("kit", home)) == pinned


def test_update_is_cancelled_without_yes_and_brings_back_a_missing_clone(tmp_path, home, capsys):
    source = tmp_path / "repo"
    url = repository(source)
    assert main(["install", url, "-y"], home=home) == 0
    before = head_of(repo_dir("kit", home))
    commit_command(source, "deploy")
    assert main(["update"], home=home, stdin=io.StringIO("")) == 1
    assert head_of(repo_dir("kit", home)) == before
    assert leftovers(home) == []

    from mosscode.plugin_command import remove_tree

    remove_tree(repo_dir("kit", home))
    assert "moss plugin update kit" in installed(home).notices[0]
    assert main(["update", "kit", "-y"], home=home) == 0
    assert head_of(repo_dir("kit", home)) == head_of(source)
    assert main(["update", "missing"], home=home) == 1
    assert "没有安装名为 missing" in capsys.readouterr().err


def test_update_skips_a_local_directory(tmp_path, home, capsys):
    install_record(home, make_plugin(tmp_path / "src"))
    assert main(["update"], home=home) == 0
    assert "原地使用" in capsys.readouterr().out


# Remove, enable, disable, list ----------------------------------------------------------------


def test_remove_deletes_a_clone_and_its_data_but_keeps_a_local_directory(tmp_path, home, capsys):
    url = repository(tmp_path / "repo")
    assert main(["install", url, "-y"], home=home) == 0
    data_dir("kit", home).mkdir(parents=True)
    (data_dir("kit", home) / "cache").write_text("x", encoding="utf-8")
    local = make_plugin(tmp_path / "src")
    assert main(["install", str(local), "--name", "mine", "-y"], home=home) == 0

    assert main(["remove", "kit"], home=home) == 0
    assert not repo_dir("kit", home).exists() and not data_dir("kit", home).exists()
    assert main(["remove", "mine"], home=home) == 0
    assert "保留" in capsys.readouterr().out
    assert (local / "skills" / "lint" / "SKILL.md").is_file()
    assert load_records(home)[0] == []
    assert main(["remove", "mine"], home=home) == 1


def test_disable_and_enable_change_what_loads(tmp_path, home, capsys):
    install_record(home, make_plugin(tmp_path / "src"))
    assert main(["disable", "kit"], home=home) == 0
    assert installed(home).plugins == ()
    assert "已停用" in report(home)
    assert main(["enable", "kit"], home=home) == 0
    assert [plugin.name for plugin in installed(home).plugins] == ["kit"]


def test_list_shows_components_problems_and_missing_plugins(tmp_path, home, capsys):
    assert "没有安装插件" in report(home)
    install_record(home, make_plugin(tmp_path / "src"))
    broken = make_plugin(tmp_path / "broken")
    (broken / "skills" / "bad").mkdir()
    (broken / "skills" / "bad" / "SKILL.md").write_text("no frontmatter", encoding="utf-8")
    install_record(home, broken, "broken")
    install_record(home, tmp_path / "gone", "gone")
    assert main(["list"], home=home) == 0
    out = capsys.readouterr().out
    assert "插件（3）" in out
    assert "Skill 1" in out and "命令 1" in out
    assert "! " in out and "bad" in out
    assert "gone" in out and "moss plugin update gone" in out
    detail = report(home, "kit")
    assert "lint" in detail and "kit-server" in detail and "状态      启用" in detail
    assert "没有安装名为 x" in report(home, "x")


def test_the_plugin_subcommand_loads_no_interface_or_sdk(tmp_path):
    home = tmp_path / "home"
    install_record(home, make_plugin(tmp_path / "src"))
    code = (
        "import sys\nfrom mosscode import launcher\n"
        "try:\n    launcher.main(['plugin', 'list'])\nexcept SystemExit:\n    pass\n"
        "print([m for m in ('textual', 'anthropic', 'openai', 'mosscode.cli', 'mosscode.tui') if m in sys.modules])"
    )
    env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home), "PYTHONIOENCODING": "utf-8"}
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, encoding="utf-8", env=env, timeout=60
    )
    assert result.returncode == 0, result.stderr
    assert "kit" in result.stdout
    assert result.stdout.strip().splitlines()[-1] == "[]"


# Doctor ---------------------------------------------------------------------------------------


def test_doctor_reports_each_plugin(tmp_path, home):
    assert doctor.check_plugins(home, tmp_path) == []
    install_record(home, make_plugin(tmp_path / "src"))
    install_record(home, tmp_path / "gone", "gone")
    install_record(home, make_plugin(tmp_path / "off"), "off", enabled=False)
    records, _ = load_records(home)
    save_records([*records, PluginRecord("cloned", "https://example.com/x.git")], home)
    items = doctor.check_plugins(home, tmp_path, which=lambda name: None)
    text = {item.text: item for item in items}
    assert any(item.level == doctor.ERROR and "找不到 git" in item.text for item in items)
    assert any(item.level == doctor.OK and item.text.startswith("插件 kit：Skill 1") for item in items)
    assert text["插件 off：已停用"].level == doctor.OK
    gone = next(item for item in items if "gone" in item.text)
    assert gone.level == doctor.ERROR and gone.fix == "moss plugin update gone"
    assert any("cloned" in item.text and item.level == doctor.ERROR for item in items)


def test_a_failed_clone_names_gits_reason_and_leaves_nothing(tmp_path, home, capsys):
    missing = (tmp_path / "missing").as_uri()
    assert main(["install", missing, "-y"], home=home) == 1
    err = capsys.readouterr().err
    assert "git clone 失败：fatal:" in err
    assert leftovers(home) == []


async def test_the_plugins_command_lists_and_shows_one(tmp_path, home, monkeypatch):
    from mosscode.conversation import Conversation
    from test_commands import FakeUI, context, run
    from test_conversation import FakeProvider

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    install_record(home, make_plugin(tmp_path / "src"))
    ui = FakeUI()
    ctx = context(Conversation(FakeProvider()), ui=ui)
    await run(ctx, "/plugins")
    assert "插件（1）" in ui.notes[-1] and "moss plugin install" in ui.notes[-1]
    await run(ctx, "/plugin kit")
    assert "kit-server" in ui.notes[-1] and "下一次启动" in ui.notes[-1]
