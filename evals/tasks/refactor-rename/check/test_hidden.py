import re
from pathlib import Path

import shop.models as models
from shop.billing import bill
from shop.report import line_summary

HERE = Path(__file__).resolve().parent


def test_new_name():
    assert models.line_total(10, 3, 0.5) == 15.0


def test_old_name_is_gone():
    assert not hasattr(models, "calc")
    for path in [*(HERE / "shop").rglob("*.py"), HERE / "test_shop.py"]:
        if path.exists():
            assert not re.search(r"\bcalc\b", path.read_text(encoding="utf-8")), path.name


def test_callers_still_work():
    assert bill([(10, 2), (5, 1, 0.2)]) == 24.0
    assert line_summary("pen", 2.5, 2) == "pen: 5.00"
