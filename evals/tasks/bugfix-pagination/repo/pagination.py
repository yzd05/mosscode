def paginate(items, page, per_page=10):
    """The items on a 1-based page; an empty list past the last page.

    page and per_page must be at least 1, otherwise ValueError.
    """
    if page < 1 or per_page < 1:
        raise ValueError("page and per_page must be at least 1")
    start = page * per_page
    return items[start : start + per_page]


def page_count(total, per_page=10):
    """How many pages total items fill; a partly filled last page counts."""
    return total // per_page
