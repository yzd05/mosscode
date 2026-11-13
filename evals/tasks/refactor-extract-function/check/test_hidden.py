import ast
import inspect

import orders
import pytest
from orders import invoice_total, price_with_tax, quote_total


def test_price_with_tax():
    assert price_with_tax(100.0, 0.1, 0.2) == pytest.approx(108.0)
    assert price_with_tax(10.0, 0.0, 0.0) == pytest.approx(10.0)


@pytest.mark.parametrize(
    "lines, discount, tax",
    [
        ([(10.0, 2), (5.0, 1)], 0.1, 0.2),
        ([(19.99, 3)], 0.0, 0.07),
        ([(3.333, 7), (0.01, 1)], 0.15, 0.0),
        ([], 0.5, 0.2),
    ],
)
def test_invoice_unchanged(lines, discount, tax):
    subtotal = sum(price * quantity for price, quantity in lines)
    assert invoice_total(lines, discount, tax) == round(subtotal * (1 - discount) * (1 + tax), 2)


@pytest.mark.parametrize(
    "unit, quantity, discount, tax, shipping",
    [(10.0, 3, 0.0, 0.1, 5.0), (19.99, 1, 0.25, 0.07, 0.0), (0.333, 9, 0.1, 0.2, 1.11)],
)
def test_quote_unchanged(unit, quantity, discount, tax, shipping):
    expected = round(unit * quantity * (1 - discount) * (1 + tax) + shipping, 2)
    assert quote_total(unit, quantity, discount, tax, shipping=shipping) == expected


@pytest.mark.parametrize("func", [invoice_total, quote_total])
def test_both_use_the_helper(func):
    tree = ast.parse(inspect.getsource(func))
    called = {node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert "price_with_tax" in called
    text = ast.unparse(tree)
    assert "1 - discount" not in text and "1 + tax_rate" not in text


def test_module_still_imports_cleanly():
    assert callable(orders.price_with_tax)
