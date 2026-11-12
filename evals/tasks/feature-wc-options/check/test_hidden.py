import pytest
from wc import main

TEXT = "one two\nthree\nfour five six"


@pytest.fixture
def sample(tmp_path):
    path = tmp_path / "sample.txt"
    path.write_text(TEXT, encoding="utf-8")
    return str(path)


def run(capsys, *argv):
    assert main(list(argv)) == 0
    return capsys.readouterr().out.strip()


def test_words_by_default(sample, capsys):
    assert run(capsys, sample) == "6"


def test_lines(sample, capsys):
    assert run(capsys, "--lines", sample) == "3"


def test_chars(sample, capsys):
    assert run(capsys, "--chars", sample) == str(len(TEXT))


def test_lines_with_a_final_newline_and_empty(tmp_path, capsys):
    ended = tmp_path / "ended.txt"
    ended.write_text("a\nb\n", encoding="utf-8")
    empty = tmp_path / "empty.txt"
    empty.write_text("", encoding="utf-8")
    assert run(capsys, "--lines", str(ended)) == "2"
    assert run(capsys, "--lines", str(empty)) == "0"


def test_both_options_are_an_error(sample):
    with pytest.raises(SystemExit) as exited:
        main(["--lines", "--chars", sample])
    assert exited.value.code == 2
