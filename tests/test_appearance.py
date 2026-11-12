import io
import re
from importlib.metadata import version
from pathlib import Path

from rich.console import Console

from mosscode.appearance import (
    LOGO_COLORS,
    MUTED_COLOR,
    STARTUP_TIP,
    blend,
    current_branch,
    logo,
    short_path,
    startup_banner,
)


def render(renderable, width=70) -> str:
    stream = io.StringIO()
    console = Console(file=stream, width=width, force_terminal=True, color_system="truecolor", legacy_windows=False)
    console.print(renderable)
    return stream.getvalue()


def plain_lines(output: str) -> list[str]:
    return [line.rstrip() for line in re.sub(r"\x1b\[[0-9;]*m", "", output).splitlines()]


def foreground(color: str) -> str:
    return "\x1b[38;2;" + ";".join(str(int(color[at : at + 2], 16)) for at in (1, 3, 5)) + "m"


def repository(folder: Path, head: str) -> Path:
    (folder / ".git").mkdir(parents=True)
    (folder / ".git" / "HEAD").write_text(head, encoding="utf-8")
    return folder


def test_blend_walks_the_stops_and_stays_inside_them():
    stops = ("#000000", "#804020", "#ffffff")
    assert [blend(stops, at) for at in (0, 0.5, 1)] == list(stops)
    assert blend(stops, 0.25) == "#402010"
    assert blend(stops, -3) == stops[0] and blend(stops, 7) == stops[-1]


def test_logo_is_a_two_line_comet_drawn_with_foreground_colors_only():
    art = logo()
    assert art.plain == "         ▁▃▅█\n▁▁▂▂▃▄▅▆▇████"
    # Every drawn cell is a block filled from the bottom, so no cell needs a background or reverse video.
    assert len(art.spans) == sum(not char.isspace() for char in art.plain)
    assert all(span.style.color and span.style.bgcolor is None and not span.style.reverse for span in art.spans)
    bottom = art.spans[-13:]
    assert bottom[0].style.color.name == LOGO_COLORS[0] and bottom[-1].style.color.name == LOGO_COLORS[-1]
    # A column has one color on both lines.
    top = {span.start: span.style.color.name for span in art.spans[:-13]}
    assert all(top[span.start - 14] == span.style.color.name for span in bottom if span.start - 14 in top)


def test_branch_is_read_from_the_nearest_repository(tmp_path):
    project = repository(tmp_path / "project", "ref: refs/heads/feature/login\n")
    nested = project / "src" / "pkg"
    nested.mkdir(parents=True)
    assert current_branch(project) == current_branch(nested) == "feature/login"
    inner = repository(project / "vendor" / "lib", "ref: refs/heads/main\n")
    assert current_branch(inner) == "main"


def test_detached_head_shows_the_short_commit(tmp_path):
    project = repository(tmp_path / "project", "0123456789abcdef0123456789abcdef01234567\n")
    assert current_branch(project) == "0123456"


def test_branch_of_a_worktree_follows_its_git_file(tmp_path):
    main = repository(tmp_path / "main", "ref: refs/heads/main\n")
    private = main / ".git" / "worktrees" / "fix"
    private.mkdir(parents=True)
    (private / "HEAD").write_text("ref: refs/heads/fix\n", encoding="utf-8")
    absolute, relative = tmp_path / "absolute", main / "relative"
    absolute.mkdir()
    relative.mkdir()
    (absolute / ".git").write_text(f"gitdir: {private}\n", encoding="utf-8")
    (relative / ".git").write_text("gitdir: ../.git/worktrees/fix\n", encoding="utf-8")
    assert current_branch(absolute) == current_branch(relative) == "fix"


def test_a_broken_git_marker_falls_back_to_the_enclosing_repository_or_nothing(tmp_path):
    project = repository(tmp_path / "project", "ref: refs/heads/main\n")
    broken = project / "broken"
    broken.mkdir()
    (broken / ".git").write_text("gitdir: missing\n", encoding="utf-8")
    assert current_branch(broken) == "main"
    assert current_branch(Path(tmp_path.anchor, "mosscode-no-such-directory", "inner")) == ""


def test_short_path_abbreviates_only_the_home_directory(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    assert short_path(home / "code" / "app") == str(Path("~", "code", "app"))
    assert short_path(home) == "~"
    assert short_path(tmp_path / "elsewhere") == str(tmp_path / "elsewhere")


def test_banner_shows_logo_name_model_directory_branch_and_tip(tmp_path, monkeypatch):
    project = repository(tmp_path / "home" / "work", "ref: refs/heads/topic\n")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.chdir(project)
    output = render(startup_banner("Demo Model"))
    where = str(Path("~", "work"))
    assert plain_lines(output) == [
        "         ▁▃▅█  MossCode v" + version("mosscode"),
        "▁▁▂▂▃▄▅▆▇████  Demo Model",
        "",
        where + "  topic",
        STARTUP_TIP,
    ]
    # The name and the model are at full strength; the version, the directory, the branch and the tip are grey.
    assert "\x1b[1mMossCode\x1b[0m" in output and "  Demo Model" in output
    muted = foreground(MUTED_COLOR)
    for secondary in (" v" + version("mosscode"), where + "  topic", STARTUP_TIP):
        assert muted + secondary + "\x1b[0m" in output
    assert "\x1b[2m" not in output and "\x1b[48;" not in output


def test_banner_outside_a_repository_has_no_branch_and_a_narrow_one_is_cut_not_wrapped(tmp_path, monkeypatch):
    monkeypatch.setattr("mosscode.appearance.current_branch", lambda directory: "")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    deep = tmp_path / ("long-directory-name-" * 3) / "inner"
    deep.mkdir(parents=True)
    monkeypatch.chdir(deep)
    assert plain_lines(render(startup_banner("Demo")))[3] == short_path(deep)
    narrow = plain_lines(render(startup_banner("A model with a rather long display name"), width=30))
    assert len(narrow) == 5 and all(len(line) <= 30 for line in narrow)
    assert narrow[1].startswith("▁▁▂▂▃▄▅▆▇████  A model") and narrow[1].endswith("…")
    assert narrow[3].endswith("…")
