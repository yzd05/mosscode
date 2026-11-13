import dataclasses
import inspect

import point
import pytest
from point import Point


def test_is_a_frozen_dataclass():
    assert dataclasses.is_dataclass(Point)
    with pytest.raises(dataclasses.FrozenInstanceError):
        Point(1, 2).x = 5


def test_behaviour_is_unchanged():
    assert Point(1, 2) == Point(1, 2) and Point(1, 2) != Point(2, 1)
    assert repr(Point(1, 2)) == "Point(x=1, y=2)"
    assert Point(0, 0).distance_to(Point(3, 4)) == 5.0
    assert Point(1, 2) != (1, 2)


def test_usable_as_a_key():
    assert {Point(1, 2): "a"}[Point(1, 2)] == "a"


def test_no_hand_written_methods():
    source = inspect.getsource(point)
    for name in ("__init__", "__eq__", "__repr__"):
        assert f"def {name}" not in source
