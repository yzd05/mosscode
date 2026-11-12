def truncate(text, width):
    """text cut to width characters, ending with "…" when cut."""
    return text if len(text) <= width else text[: width - 1] + "…"
