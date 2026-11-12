from .models import calc


def bill(lines):
    """Total of (price, quantity[, discount]) lines."""
    return round(sum(calc(*line) for line in lines), 2)
