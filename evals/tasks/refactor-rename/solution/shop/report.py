from . import models


def line_summary(name, price, quantity):
    return f"{name}: {models.line_total(price, quantity):.2f}"
