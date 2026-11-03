"""Waiting for asynchronous work in tests.

Poll for the condition instead of sleeping for a guessed time: CI runners can be many times slower
than a development machine, and a generous timeout costs nothing when the condition holds early.
"""

import asyncio
import time


async def until(predicate, timeout: float = 10.0, interval: float = 0.02, message: str = "condition not reached"):
    """Returns once predicate() is true; fails after timeout seconds."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(message)
        await asyncio.sleep(interval)
