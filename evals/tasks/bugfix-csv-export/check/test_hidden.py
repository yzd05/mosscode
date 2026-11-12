import csv
import io

from report import to_csv

ROWS = [
    ["name", "note"],
    ["Ann", "likes, commas"],
    ["Bob", 'says "hi"'],
    ["Cy", "two\nlines"],
    ["", "empty first"],
]


def test_round_trip():
    assert list(csv.reader(io.StringIO(to_csv(ROWS), newline=""))) == ROWS


def test_plain_fields_unchanged():
    assert to_csv([["a", "b"], ["c", "d"]]) == "a,b\nc,d\n"


def test_no_rows():
    assert to_csv([]) == ""
