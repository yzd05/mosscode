import csv
import io


def to_csv(rows):
    """Rows (lists of strings) as CSV text, one line per row, each ending with a newline."""
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="\n").writerows(rows)
    return buffer.getvalue()
