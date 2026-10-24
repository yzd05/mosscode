"""Stage 9a: instruction layers and @include expansion (docs/specs/chapter-09-memory/9a-instructions.md)."""

import os
import subprocess

import pytest

from mosscode.conversation import Conversation
from mosscode.prompts import EXPANDED_LIMIT, INCLUDE_DEPTH, instructions_section, load_instructions
from test_agent import SequenceProvider, response


def tree(tmp_path):
    home, project = tmp_path / "home", tmp_path / "work" / "app"
    (home / ".mosscode").mkdir(parents=True)
    project.mkdir(parents=True)
    return home, project


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8")
    return path


def load(project, home, tmp_path):
    """Only the files under tmp_path: the checkout's own ancestors may hold instruction files."""
    files, problems = load_instructions(project, home)
    return [f for f in files if tmp_path in f.path.resolve().parents], problems


# AC1 (I1-I3)

def test_layers_run_from_user_to_dot_mosscode(tmp_path):
    home, project = tree(tmp_path)
    write(home / ".mosscode" / "AGENTS.md", "用户级")
    write(project.parent / "AGENTS.md", "上级")
    write(project / "AGENTS.md", "项目根")
    write(project / ".mosscode" / "AGENTS.md", "专用")
    files, problems = load(project, home, tmp_path)
    assert [f.text for f in files] == ["用户级", "上级", "项目根", "专用"] and not problems
    assert "`.mosscode/AGENTS.md` 优先于同目录的 `AGENTS.md`" in instructions_section(files)
    (project / "AGENTS.md").unlink()
    (project.parent / "AGENTS.md").unlink()
    files, _ = load(project, home, tmp_path)
    assert [f.text for f in files] == ["用户级", "专用"]


# AC2 (N1-N3)

def test_references_expand_relative_to_their_own_file(tmp_path):
    home, project = tree(tmp_path)
    write(project / "AGENTS.md", '开头\n@docs/style.md\n  @"my notes/a b.md"  \n结尾')
    style = write(project / "docs" / "style.md", "风格\n@rules/naming.md\n")
    naming = write(project / "docs" / "rules" / "naming.md", "命名")
    spaced = write(project / "my notes" / "a b.md", "空格路径")
    [file], problems = load(project, home, tmp_path)
    assert file.text == "开头\n风格\n命名\n空格路径\n结尾" and not problems
    assert file.includes == (style.resolve(), naming.resolve(), spaced.resolve())


def test_code_fences_and_inline_mentions_are_left_alone(tmp_path):
    home, project = tree(tmp_path)
    write(project / "x.md", "不应出现")
    text = ("```python\n@x.md\n@dataclass\n```\n~~~\n@x.md\n~~~\n@dataclass\n"
            "联系 a@b.com，参考 @x.md 一节\n`@x.md`")
    write(project / "AGENTS.md", text)
    [file], problems = load(project, home, tmp_path)
    assert file.text == text and not file.includes and not problems


def test_user_file_may_reference_its_own_directory_with_tilde(tmp_path):
    home, project = tree(tmp_path)
    write(home / ".mosscode" / "AGENTS.md", "@~/.mosscode/rules/a.md\n@~/other.md")
    write(home / ".mosscode" / "rules" / "a.md", "公共规范")
    write(home / "other.md", "家目录秘密")
    [file], problems = load(project, home, tmp_path)
    assert file.text == "公共规范\n（引用超出允许的目录，未展开：~/other.md）"
    assert "家目录秘密" not in file.text
    assert problems == [f"项目指令引用未展开：{home / '.mosscode' / 'AGENTS.md'} 中的 @~/other.md（超出允许的目录）"]


# AC3 (N4-N6)

def test_reference_depth_is_limited(tmp_path):
    home, project = tree(tmp_path)
    write(project / "AGENTS.md", "@c1.md")
    for level in range(1, 8):
        write(project / f"c{level}.md", f"第{level}层\n@c{level + 1}.md" if level < 7 else "第7层")
    [file], problems = load(project, home, tmp_path)
    assert f"第{INCLUDE_DEPTH}层" in file.text and f"第{INCLUDE_DEPTH + 1}层" not in file.text
    assert file.text.endswith(f"（引用层级超过 {INCLUDE_DEPTH} 层，未展开：c{INCLUDE_DEPTH + 1}.md）")
    assert problems == [f"项目指令引用未展开：c{INCLUDE_DEPTH}.md 中的 @c{INCLUDE_DEPTH + 1}.md"
                        f"（引用层级超过 {INCLUDE_DEPTH} 层）"]


def test_cycles_are_reported_and_shared_files_load_once(tmp_path):
    home, project = tree(tmp_path)
    write(project / "AGENTS.md", "@a.md\n@b.md")
    write(project / "a.md", "A\n@shared.md\n@b.md")
    write(project / "b.md", "B\n@a.md\n@shared.md\n@AGENTS.md\n@.mosscode/AGENTS.md")
    write(project / "shared.md", "共享规范")
    write(project / ".mosscode" / "AGENTS.md", "专用")
    [root_file, dot_file], problems = load(project, home, tmp_path)
    assert root_file.text.count("共享规范") == 1 and root_file.text.count("专用") == 0 and dot_file.text == "专用"
    assert root_file.text.count("B") == 1
    assert "（循环引用，未展开：a.md）" in root_file.text and "（循环引用，未展开：AGENTS.md）" in root_file.text
    assert "（已在其他位置加载：shared.md）" in root_file.text
    assert "（已在其他位置加载：.mosscode/AGENTS.md）" in root_file.text
    assert "（已在其他位置加载：b.md）" in root_file.text  # AGENTS.md's own @b.md after a.md pulled it in
    assert sorted(problems) == ["项目指令引用未展开：b.md 中的 @AGENTS.md（循环引用）",
                                "项目指令引用未展开：b.md 中的 @a.md（循环引用）"]


# AC4 (N7)

def test_references_cannot_leave_their_layer_boundary(tmp_path):
    home, project = tree(tmp_path)
    outside = write(project.parent / "outside.md", "外部秘密")
    write(project / "AGENTS.md", f"@../outside.md\n@{outside}")
    write(project / ".mosscode" / "AGENTS.md", "@../../outside.md\n@../docs/x.md")
    write(project / "docs" / "x.md", "项目内文档")
    [root_file, dot_file], problems = load(project, home, tmp_path)
    assert "外部秘密" not in root_file.text + dot_file.text
    assert dot_file.text.endswith("项目内文档")
    assert len(problems) == 3 and all("超出允许的目录" in p for p in problems)


def test_parent_layer_may_reference_its_own_directory(tmp_path):
    home, project = tree(tmp_path)
    write(project.parent / "AGENTS.md", "@shared/rules.md")
    write(project.parent / "shared" / "rules.md", "仓库规范")
    [file], problems = load(project, home, tmp_path)
    assert file.text == "仓库规范" and not problems


@pytest.mark.skipif(os.name != "nt", reason="Windows junction 行为")
def test_junction_out_of_the_project_is_not_followed(tmp_path):
    home, project = tree(tmp_path)
    write(tmp_path / "secret" / "s.md", "junction 秘密")
    link = project / "link"
    created = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(tmp_path / "secret")], capture_output=True)
    assert created.returncode == 0, created.stderr
    try:
        write(project / "AGENTS.md", "@link/s.md")
        [file], problems = load(project, home, tmp_path)
        assert "junction 秘密" not in file.text and "超出允许的目录" in problems[0]
    finally:
        os.rmdir(link)


# AC5 (N8, N9)

def test_unreadable_references_are_replaced_and_the_rest_loads(tmp_path):
    home, project = tree(tmp_path)
    write(project / "AGENTS.md", "开头\n@missing.md\n@docs/\n@bin.dat\n@gbk.md\n@ok.md")
    (project / "docs").mkdir()
    write(project / "bin.dat", b"\x00\x01\x02")
    write(project / "gbk.md", "中文".encode("gbk"))
    write(project / "ok.md", "正常")
    [file], problems = load(project, home, tmp_path)
    assert file.text == ("开头\n（引用无法读取：missing.md）\n（引用无法读取：docs/）\n（引用无法读取：bin.dat）\n"
                         "（引用无法读取：gbk.md）\n正常")
    assert [p.rsplit("（", 1)[1] for p in problems] == ["文件不存在或不是文件）", "文件不存在或不是文件）",
                                                         "不是文本文件）", "不是 UTF-8 文本）"]


def test_expanded_text_is_capped(tmp_path):
    home, project = tree(tmp_path)
    write(project / "AGENTS.md", "@a.md\n@b.md\n@c.md")
    for name in "abc":
        write(project / f"{name}.md", name * 30_000)
    [file], _ = load(project, home, tmp_path)
    assert file.overflow and not file.truncated
    body, note = file.text.rsplit("\n", 1)
    assert note == "（引用展开后超过 64 KiB，其余内容已截断）" and len(body.encode()) <= EXPANDED_LIMIT


# AC6 (N10, N11)

async def test_notices_list_references_and_instructions_stay_frozen(tmp_path):
    home, project = tree(tmp_path)
    write(project / "AGENTS.md", "@docs/style.md\n@../outside.md")
    style = write(project / "docs" / "style.md", "旧规范")
    write(project.parent / "outside.md", "外部")
    provider = SequenceProvider(response("好"), response("好"), response("好"))
    conv = Conversation(provider, context_root=project, instructions_home=home)
    notices = await conv.start()
    assert any(n.startswith("已加载项目指令：") and "AGENTS.md（引用 docs/style.md）" in n for n in notices)
    assert "项目指令引用未展开：AGENTS.md 中的 @../outside.md（超出允许的目录）" in notices
    await conv.ask("一")
    style.write_text("新规范", encoding="utf-8")
    await conv.ask("二")
    first, second = (request[1].stable for request in provider.requests[:2])
    assert first == second and "旧规范" in second
    await conv.clear()
    await conv.ask("三")
    assert "新规范" in provider.requests[2][1].stable
