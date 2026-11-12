from shop.billing import bill
from shop.models import calc


def test_calc():
    assert calc(2.0, 3) == 6.0


def test_bill():
    assert bill([(2.0, 3), (1.0, 1)]) == 7.0
