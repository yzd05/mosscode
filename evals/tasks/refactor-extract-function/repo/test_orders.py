from orders import invoice_total, quote_total


def test_invoice():
    assert invoice_total([(10.0, 2), (5.0, 1)], 0.1, 0.2) == 27.0


def test_quote():
    assert quote_total(10.0, 3, 0.0, 0.1, shipping=5.0) == 38.0
