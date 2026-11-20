"""Installed package version helper without importing CLI or surface modules."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _installed_version


def version() -> str:
    try:
        return _installed_version("mosscode")
    except PackageNotFoundError:  # Run from a source tree that was never installed.
        return "0+unknown"
