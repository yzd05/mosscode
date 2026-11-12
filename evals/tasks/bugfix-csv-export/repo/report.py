def to_csv(rows):
    """Rows (lists of strings) as CSV text, one line per row, each ending with a newline."""
    return "".join(",".join(row) + "\n" for row in rows)
