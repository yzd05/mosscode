"""MossCode: a coding agent in the terminal."""


def __getattr__(name: str):
    """`mosscode.__version__` from the installed metadata, read only when asked for (refinement 08 IN3)."""
    if name == "__version__":
        from .launcher import version
        return version()
    raise AttributeError(f"module 'mosscode' has no attribute {name!r}")
