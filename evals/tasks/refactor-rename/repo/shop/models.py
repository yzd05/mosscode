def calc(price, quantity, discount=0.0):
    """Price of one order line, to the cent."""
    return round(price * quantity * (1 - discount), 2)
