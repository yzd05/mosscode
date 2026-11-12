def price_with_tax(subtotal, discount, tax_rate):
    """subtotal less a fractional discount, plus tax; not rounded."""
    return subtotal * (1 - discount) * (1 + tax_rate)


def invoice_total(lines, discount, tax_rate):
    """Sum of price * quantity over (price, quantity) lines, less a fractional discount, plus tax; to the cent."""
    subtotal = sum(price * quantity for price, quantity in lines)
    return round(price_with_tax(subtotal, discount, tax_rate), 2)


def quote_total(unit_price, quantity, discount, tax_rate, shipping=0.0):
    """One product's price less a fractional discount, plus tax, plus untaxed shipping; to the cent."""
    return round(price_with_tax(unit_price * quantity, discount, tax_rate) + shipping, 2)
