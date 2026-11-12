from . import models


def line_summary(name, price, quantity):
    return f"{name}: {models.calc(price, quantity):.2f}"
