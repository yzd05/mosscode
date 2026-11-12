import copy

from settings import merge_config

BASE = {"db": {"host": "localhost", "port": 5432}, "debug": False, "plugins": ["a"]}


def test_nested_dicts_merge():
    out = merge_config(copy.deepcopy(BASE), {"db": {"port": 6543}, "debug": True})
    assert out == {"db": {"host": "localhost", "port": 6543}, "debug": True, "plugins": ["a"]}


def test_deeply_nested():
    assert merge_config({"a": {"b": {"c": 1, "d": 2}}}, {"a": {"b": {"d": 3}}}) == {"a": {"b": {"c": 1, "d": 3}}}


def test_lists_and_scalars_are_replaced():
    assert merge_config(copy.deepcopy(BASE), {"plugins": ["b"]})["plugins"] == ["b"]
    assert merge_config({"x": 1}, {"x": {"y": 2}}) == {"x": {"y": 2}}
    assert merge_config({"x": {"y": 2}}, {"x": 1}) == {"x": 1}


def test_arguments_are_left_alone():
    base = copy.deepcopy(BASE)
    override = {"db": {"port": 1}, "new": {"k": "v"}, "plugins": ["b"]}
    snapshot = copy.deepcopy((base, override))
    out = merge_config(base, override)
    assert (base, override) == snapshot
    out["db"]["host"] = "changed"
    out["new"]["k"] = "changed"
    out["plugins"].append("c")
    assert (base, override) == snapshot
