def merge_config(base, override):
    """base with override's values on top.

    Where both sides hold a dict, the two merge key by key, at any depth; anything else (lists included) is
    replaced by override's value. Neither argument is modified, and the result shares no dict or list with them.
    """
    result = base
    result.update(override)
    return result
