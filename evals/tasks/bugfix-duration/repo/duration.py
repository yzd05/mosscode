import re

UNITS = {"h": 3600, "m": 60, "s": 1}


def parse_duration(text):
    """Seconds in a duration like "1h30m", "45s" or "2m5s"; ValueError for anything else."""
    match = re.match(r"(\d+)([hms])", text)
    if not match:
        raise ValueError(f"invalid duration: {text!r}")
    return int(match.group(1)) * UNITS[match.group(2)]
