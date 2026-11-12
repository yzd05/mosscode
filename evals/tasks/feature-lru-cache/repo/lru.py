class LRUCache:
    """A mapping that holds at most capacity entries and evicts the least recently used one.

    get(key) returns the value, or None when absent, and counts as a use. put(key, value) inserts or updates the
    entry, also a use, and evicts the least recently used entry when over capacity. len() is the number of
    entries. A capacity below 1 is a ValueError.
    """

    def __init__(self, capacity):
        raise NotImplementedError
