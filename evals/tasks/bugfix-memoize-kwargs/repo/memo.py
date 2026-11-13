import functools


def memoize(func):
    """Cache results by the call's arguments."""
    cache = {}

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        if args not in cache:
            cache[args] = func(*args, **kwargs)
        return cache[args]

    return wrapper
