import pytest
from pagination import paginate


def test_rejects_page_zero():
    with pytest.raises(ValueError):
        paginate([1, 2, 3], 0)
