from shop.billing import bill
from shop.models import line_total


def test_line_total():
    assert line_total(2.0, 3) == 6.0


def test_bill():
    assert bill([(2.0, 3), (1.0, 1)]) == 7.0
