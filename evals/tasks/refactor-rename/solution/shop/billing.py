from .models import line_total


def bill(lines):
    """Total of (price, quantity[, discount]) lines."""
    return round(sum(line_total(*line) for line in lines), 2)
