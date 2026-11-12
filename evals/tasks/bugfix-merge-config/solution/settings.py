import copy


def merge_config(base, override):
    """base with override's values on top.

    Where both sides hold a dict, the two merge key by key, at any depth; anything else (lists included) is
    replaced by override's value. Neither argument is modified, and the result shares no dict or list with them.
    """
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_config(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result
