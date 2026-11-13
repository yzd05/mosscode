def invoice_total(lines, discount, tax_rate):
    """Sum of price * quantity over (price, quantity) lines, less a fractional discount, plus tax; to the cent."""
    subtotal = sum(price * quantity for price, quantity in lines)
    discounted = subtotal * (1 - discount)
    return round(discounted * (1 + tax_rate), 2)


def quote_total(unit_price, quantity, discount, tax_rate, shipping=0.0):
    """One product's price less a fractional discount, plus tax, plus untaxed shipping; to the cent."""
    subtotal = unit_price * quantity
    discounted = subtotal * (1 - discount)
    return round(discounted * (1 + tax_rate) + shipping, 2)
