import re


def truncate(text, width):
    """text cut to width characters, ending with "…" when cut."""
    return text if len(text) <= width else text[: width - 1] + "…"


def slugify(title):
    """Lower case, letters and digits kept, every other run of characters one hyphen, no hyphen at either end."""
    return re.sub(r"[\W_]+", "-", title.lower()).strip("-")
