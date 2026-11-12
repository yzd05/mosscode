import pytest
from lru import LRUCache


def test_get_and_put():
    cache = LRUCache(2)
    cache.put("a", 1)
    assert cache.get("a") == 1
    assert cache.get("missing") is None


def test_evicts_the_least_recently_used():
    cache = LRUCache(2)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.put("c", 3)
    assert cache.get("a") is None
    assert (cache.get("b"), cache.get("c")) == (2, 3)


def test_get_counts_as_a_use():
    cache = LRUCache(2)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.get("a")
    cache.put("c", 3)
    assert cache.get("b") is None
    assert cache.get("a") == 1


def test_update_counts_as_a_use_and_does_not_grow():
    cache = LRUCache(2)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.put("a", 10)
    assert len(cache) == 2
    cache.put("c", 3)
    assert cache.get("a") == 10 and cache.get("b") is None
    assert len(cache) == 2


def test_capacity_one():
    cache = LRUCache(1)
    cache.put("a", 1)
    cache.put("b", 2)
    assert cache.get("a") is None and cache.get("b") == 2


def test_invalid_capacity():
    with pytest.raises(ValueError):
        LRUCache(0)
