import re

UNITS = {"h": 3600, "m": 60, "s": 1}
PART = re.compile(r"(\d+)([hms])")


def parse_duration(text):
    """Seconds in a duration like "1h30m", "45s" or "2m5s"; ValueError for anything else."""
    if not re.fullmatch(r"(?:\d+[hms])+", text):
        raise ValueError(f"invalid duration: {text!r}")
    return sum(int(number) * UNITS[unit] for number, unit in PART.findall(text))
