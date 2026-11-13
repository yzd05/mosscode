import pytest
from duration import parse_duration


@pytest.mark.parametrize(
    "text, seconds",
    [("1h30m", 5400), ("45s", 45), ("2m5s", 125), ("1h", 3600), ("1h0m30s", 3630), ("90m", 5400)],
)
def test_valid(text, seconds):
    assert parse_duration(text) == seconds


@pytest.mark.parametrize("text", ["", "abc", "10", "5x", "1h30", "m5", "1h 30m"])
def test_invalid(text):
    with pytest.raises(ValueError):
        parse_duration(text)
