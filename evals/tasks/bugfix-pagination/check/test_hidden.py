import pytest
from pagination import page_count, paginate

ITEMS = list(range(25))


def test_first_page():
    assert paginate(ITEMS, 1) == list(range(10))


def test_middle_and_last_pages():
    assert paginate(ITEMS, 2) == list(range(10, 20))
    assert paginate(ITEMS, 3) == [20, 21, 22, 23, 24]


def test_past_the_last_page():
    assert paginate(ITEMS, 4) == []


def test_page_size():
    assert paginate(ITEMS, 2, per_page=7) == list(range(7, 14))


def test_invalid_arguments():
    with pytest.raises(ValueError):
        paginate(ITEMS, 0)
    with pytest.raises(ValueError):
        paginate(ITEMS, 1, per_page=0)


def test_page_count():
    assert page_count(25) == 3
    assert page_count(20) == 2
    assert page_count(0) == 0
    assert page_count(1, per_page=7) == 1
