from collections import OrderedDict


class LRUCache:
    """A mapping that holds at most capacity entries and evicts the least recently used one.

    get(key) returns the value, or None when absent, and counts as a use. put(key, value) inserts or updates the
    entry, also a use, and evicts the least recently used entry when over capacity. len() is the number of
    entries. A capacity below 1 is a ValueError.
    """

    def __init__(self, capacity):
        if capacity < 1:
            raise ValueError("capacity must be at least 1")
        self.capacity = capacity
        self._entries = OrderedDict()

    def get(self, key):
        if key not in self._entries:
            return None
        self._entries.move_to_end(key)
        return self._entries[key]

    def put(self, key, value):
        self._entries[key] = value
        self._entries.move_to_end(key)
        if len(self._entries) > self.capacity:
            self._entries.popitem(last=False)

    def __len__(self):
        return len(self._entries)
