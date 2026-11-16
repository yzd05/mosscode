"""`moss plugin`: sources, marketplaces, confirmation, update, remove and the doctor section; git only on local repos."""

import argparse
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from mosscode import doctor
from mosscode.plugin_command import clone, main, normalize, report
from mosscode.plugins import (
    PluginError,
    PluginRecord,
    data_dir,
    installed,
    load_project_records,
    load_records,
    plugins_home,
    project_clone_dir,
    project_plugins,
    project_records_path,
    read_head,
    records_path,
    repo_dir,
    save_records,
)
from mosscode.skills import SkillCatalog
from test_plugins import install_record, make_plugin, write_skill


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
    commit_command(tmp_path / "repo", "Bad.Name")
    git(tmp_path / "repo", "tag", "v1")
    assert main(["install", url, "--ref", "v1", "-y"], home=home) == 0
    out = capsys.readouterr().out
    assert repo_dir("kit", home).as_posix() in out  # The server path as it will be, not the temporary clone.
    assert str(repo_dir("kit", home) / "commands" / "Bad.Name.md") in out  # So is a problem's file.
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


# Project plugins ------------------------------------------------------------------------------


@pytest.fixture
def project(tmp_path):
    path = tmp_path / "project"
    path.mkdir()
    return path


def declare(project: Path, *entries: dict) -> None:
    """A project file as a teammate committed it."""
    path = project_records_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"plugins": list(entries)}), encoding="utf-8")


def declared(project: Path) -> list[dict]:
    return yaml.safe_load(project_records_path(project).read_text(encoding="utf-8"))["plugins"]


LATER = {"name": "later", "source": "https://example.com/later.git"}


def test_a_project_install_declares_the_plugin_and_keeps_its_clone_apart(tmp_path, home, project, capsys):
    url = repository(tmp_path / "repo")
    assert main(["install", url, "--project", "-y"], home=home, root=project) == 0
    assert "提交这个文件" in capsys.readouterr().out
    assert declared(project) == [{"name": "kit", "source": url}]
    [record] = load_project_records(project)[0]
    assert record.scope == "project" and record.base(home) == project_clone_dir("kit", url, None, home)
    assert head_of(record.base(home)) == head_of(tmp_path / "repo")
    assert load_records(home)[0] == [] and not repo_dir("kit", home).exists()
    assert main(["install", url, "--project", "-y"], home=home, root=project) == 1
    assert "项目已声明名为 kit" in capsys.readouterr().err


def test_a_project_install_needs_a_git_source(tmp_path, home, project, capsys):
    source = make_plugin(tmp_path / "src")
    assert main(["install", str(source), "--project", "-y"], home=home, root=project) == 1
    assert "需要 git 来源" in capsys.readouterr().err
    assert not project_records_path(project).exists()


def test_install_without_a_source_installs_what_the_project_declares(tmp_path, home, project, capsys):
    assert main(["install"], home=home, root=project) == 1
    assert "没有声明插件" in capsys.readouterr().err
    url = repository(tmp_path / "repo")
    declare(project, {"name": "kit", "source": url})
    assert "尚未安装（kit）" in project_plugins(project, home).notices[-1]
    assert main(["install"], home=home, root=project, stdin=io.StringIO("")) == 1  # Not a terminal, no -y.
    assert project_plugins(project, home).missing and leftovers(home) == []
    assert main(["install"], home=home, root=project, stdin=Terminal("y\n")) == 0
    out = capsys.readouterr().out
    assert "插件 kit 1.0.0" in out and "以你的权限运行" in out and "确认后生效" in out
    state = project_plugins(project, home)
    assert not state.missing and [plugin.name for plugin in state.plugins] == ["kit"]
    assert main(["install", "-y"], home=home, root=project) == 0
    assert "插件 kit（项目级） 已安装" in capsys.readouterr().out
    assert main(["install", "--ref", "v1"], home=home, root=project) == 1


def test_project_plugins_load_once_trusted_and_ask_again_after_a_change(tmp_path, home, project, capsys):
    source = tmp_path / "repo"
    url = repository(source)
    assert main(["install", url, "--project", "-y"], home=home, root=project) == 0
    state = project_plugins(project, home)
    assert not state.trusted(home) and installed(home, project).plugins == ()
    state.trust(home)
    assert [plugin.name for plugin in installed(home, project).plugins] == ["kit"]
    assert SkillCatalog.load(project, home, builtin=tmp_path / "none").get("lint").source == "plugin"

    path = project_records_path(project)
    path.write_text(path.read_text(encoding="utf-8") + "# 改过\n", encoding="utf-8")  # The declarations changed.
    assert not project_plugins(project, home).trusted(home) and installed(home, project).plugins == ()
    project_plugins(project, home).trust(home)

    commit_command(source, "deploy")  # So did the commit.
    assert main(["update", "-y"], home=home, root=project) == 0
    assert "+ 命令 /deploy" in capsys.readouterr().out
    assert not project_plugins(project, home).trusted(home)


def test_a_project_plugin_replaces_a_same_named_user_plugin(tmp_path, home, project):
    user = make_plugin(tmp_path / "mine")
    write_skill(user / "skills", "only-mine", "只有用户级的版本有")
    install_record(home, user)
    url = repository(tmp_path / "repo")
    assert main(["install", url, "--project", "-y"], home=home, root=project) == 0
    assert SkillCatalog.load(project, home, builtin=tmp_path / "none").get("only-mine") is not None
    project_plugins(project, home).trust(home)
    plugins = installed(home, project).plugins
    assert [(plugin.name, plugin.record.scope) for plugin in plugins] == [("kit", "project")]
    assert SkillCatalog.load(project, home, builtin=tmp_path / "none").get("only-mine") is None
    assert "覆盖同名的用户级插件 kit" in report(home, root=project)


def test_project_declarations_cannot_pass_options_to_git_or_point_at_this_machine(tmp_path, home, project):
    declare(
        project,
        {"name": "dash", "source": "-uevil@example.com:"},
        {"name": "ref", "source": "https://example.com/x.git", "ref": "--upload-pack=evil"},
        {"name": "here", "source": str(make_plugin(tmp_path / "src"))},
        {"name": "flag", "source": "https://example.com/x.git", "enabled": False},
    )
    records, problems = load_project_records(project)
    assert records == []
    text = "\n".join(problems)
    assert "source 不能以 - 开头" in text and "ref 不能以 - 开头" in text
    assert "必须是 git 地址" in text and "未知字段 enabled" in text
    records_path(home).parent.mkdir(parents=True)
    records_path(home).write_text("plugins:\n- {name: x, source: '-uevil@example.com:'}\n", encoding="utf-8")
    assert "不能以 - 开头" in load_records(home)[1][0]
    with pytest.raises(PluginError, match="不能以 - 开头"):
        normalize("-uevil@example.com:")
    # Whatever reaches git is a repository, never an option.
    with pytest.raises(PluginError, match="git clone 失败"):
        clone("--version", None, home)
    assert leftovers(home) == []


def test_remove_project_drops_the_declaration_and_the_clone(tmp_path, home, project, capsys):
    url = repository(tmp_path / "repo")
    assert main(["install", url, "--project", "-y"], home=home, root=project) == 0
    clone_dir = load_project_records(project)[0][0].base(home)
    assert main(["remove", "kit"], home=home, root=project) == 1
    assert "remove --project kit" in capsys.readouterr().err
    assert main(["disable", "kit"], home=home, root=project) == 1
    assert main(["remove", "--project", "kit"], home=home, root=project) == 0
    assert load_project_records(project)[0] == [] and not clone_dir.exists()
    assert main(["remove", "--project", "kit"], home=home, root=project) == 1


def test_list_and_details_show_project_plugins_and_whether_they_are_trusted(tmp_path, home, project):
    url = repository(tmp_path / "repo")
    assert main(["install", url, "--project", "-y"], home=home, root=project) == 0
    declare(project, *declared(project), LATER)
    text = report(home, root=project)
    assert "项目插件（2" in text and "未确认" in text and "kit 1.0.0 项目级" in text
    assert "later  项目级" in text and "运行 moss plugin install" in text
    project_plugins(project, home).trust(home)
    assert "已确认" in report(home, root=project)
    detail = report(home, "kit", root=project)
    assert "kit-server" in detail and "项目级" in detail and "已确认" in detail


def test_doctor_reports_project_plugins(tmp_path, home, project):
    url = repository(tmp_path / "repo")
    assert main(["install", url, "--project", "-y"], home=home, root=project) == 0
    declare(project, *declared(project), LATER)
    items = doctor.check_plugins(home, project)
    texts = {item.text: item for item in items}
    assert texts["项目插件 later 尚未安装"].fix == "moss plugin install"
    assert texts["项目插件尚未确认，不会加载"].level == doctor.WARN
    assert any(item.level == doctor.OK and item.text.startswith("项目插件 kit：Skill 1") for item in items)


def test_read_head_follows_loose_and_packed_refs(tmp_path):
    source = tmp_path / "repo"
    repository(source)
    assert read_head(source) == head_of(source)
    git(source, "pack-refs", "--all")
    assert not (source / ".git" / "refs" / "heads" / "main").exists()
    assert read_head(source) == head_of(source)
    git(source, "checkout", "--quiet", "--detach")
    assert read_head(source) == head_of(source)
    assert read_head(tmp_path / "nowhere") == ""


# Starting a session with project plugins ------------------------------------------------------


def startup(project: Path, home: Path, monkeypatch, prompt=None, trust=False, answer=None):
    from mosscode import cli

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    asked = []

    class Panel:
        def __init__(self, names):
            asked.append(names)

        def run(self, inline, mouse):
            return answer

    monkeypatch.setattr(cli, "PluginTrustApp", Panel)
    args = argparse.Namespace(prompt=prompt, trust_project_plugins=trust)
    return cli.load_plugins(args, project), asked


@pytest.mark.parametrize("answer,loaded", [(True, True), (False, False), (None, False)])
def test_the_interface_asks_once_before_loading_project_plugins(
    tmp_path, home, project, monkeypatch, capsys, answer, loaded
):
    url = repository(tmp_path / "repo")
    assert main(["install", url, "--project", "-y"], home=home, root=project) == 0
    capsys.readouterr()
    result, asked = startup(project, home, monkeypatch, answer=answer)
    assert asked == [["kit"]] and "以你的权限运行" in capsys.readouterr().out
    assert [plugin.name for plugin in result.plugins] == (["kit"] if loaded else [])
    assert project_plugins(project, home).trusted(home) is loaded
    if loaded:
        assert startup(project, home, monkeypatch, answer=False)[1] == []  # Trusted: not asked again.
    else:
        assert any("下次启动时会再次询问" in notice for notice in result.notices)


def test_headless_runs_load_project_plugins_only_when_trusted(tmp_path, home, project, monkeypatch):
    url = repository(tmp_path / "repo")
    assert main(["install", url, "--project", "-y"], home=home, root=project) == 0
    result, asked = startup(project, home, monkeypatch, prompt="hi")
    assert asked == [] and result.plugins == ()
    assert any("--trust-project-plugins" in notice for notice in result.notices)
    result, asked = startup(project, home, monkeypatch, prompt="hi", trust=True)
    assert [plugin.name for plugin in result.plugins] == ["kit"]
    assert not project_plugins(project, home).trusted(home)  # Trusted for this run only.


def test_a_missing_declared_plugin_is_named_at_startup(home, project, monkeypatch):
    declare(project, LATER)
    result, asked = startup(project, home, monkeypatch, prompt="hi")
    assert asked == [] and "运行 moss plugin install 安装" in result.notices[-1]
