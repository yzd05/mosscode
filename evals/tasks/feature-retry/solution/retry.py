"""Helpers for calling functions that fail now and then."""

import functools


def retry(attempts, exceptions=(Exception,)):
    """Call the function up to attempts times while it raises one of exceptions; the last error propagates."""
    if attempts < 1:
        raise ValueError("attempts must be at least 1")

    def decorate(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(1, attempts + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions:
                    if attempt == attempts:
                        raise

        return wrapper

    return decorate
