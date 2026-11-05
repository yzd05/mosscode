"""What commands the model runs see of this process: secrets removed from the environment, a private temp directory.

Commands the model writes (and Skill tools it calls) run with the user's rights, so whatever sits in the inherited
environment is one `env` away from the model, the transcript and anything the command sends out. Variables are
removed by name and by the shape of their value rather than kept from a fixed list, because the variables toolchains
need differ too much between machines (MSVC, MSYS2, language managers) for a list to stay complete.
"""

from __future__ import annotations

import atexit
import fnmatch
import os
import re
import secrets
import shutil
import time
from collections.abc import Iterable, Mapping
from pathlib import Path

from .credentials import child_environment

# Parts of a variable name that mark a credential. AUTH alone is left out: XAUTHORITY is a path GUI tools need.
SECRET_NAME = re.compile(
    r"TOKEN(?!IZER)|SECRET|PASSWORD|PASSWD|PASSPHRASE|CREDENTIAL|API_?KEY|ACCESS_?KEY|PRIVATE_?KEY|(^|_)KEY$|(^|_)PAT$|COOKIE",
    re.IGNORECASE,
)
SECRET_VALUE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|^eyJ[\w-]{8,}\.eyJ[\w-]{8,}\.[\w-]+$"  # JWT
    r"|^(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}$|^github_pat_\w{20,}$"
    r"|^glpat-[\w-]{20,}$|^xox[abposr]-[\w-]{10,}$|^sk-[\w-]{20,}$|^npm_[A-Za-z0-9]{30,}$"
    r"|^AKIA[0-9A-Z]{16}$|^AIza[\w-]{30,}$"
    r"|^[a-z][a-z0-9+.-]*://[^/:@\s]+:[^/@\s]+@"  # A URL with a password in it.
)
# Proxies stay even with a password in the URL: without them nothing in the command reaches the network.
PROXY_NAMES = {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "FTP_PROXY"}
OWN_PREFIX = "MOSSCODE_"  # Set by MossCode for the command itself, such as the command text for Git Bash.
TEMP_NAMES = ("TMPDIR", "TEMP", "TMP")
STALE_SECONDS = 7 * 24 * 3600  # Temp directories a crashed process left behind.

_keep: tuple[str, ...] = ()
_drop: tuple[str, ...] = ()
_temp: Path | None = None


def configure(keep: Iterable[str] = (), drop: Iterable[str] = ()) -> None:
    """Names (with * wildcards) from command_env in config.yaml; keep wins over every removal rule."""
    global _keep, _drop
    _keep, _drop = tuple(keep), tuple(drop)


def _matches(name: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(name.upper(), pattern.upper()) for pattern in patterns)


def removed(name: str, value: str) -> bool:
    if name.startswith(OWN_PREFIX) or _matches(name, _keep):
        return False
    if _matches(name, _drop):
        return True
    if name.upper() in PROXY_NAMES:
        return False
    return bool(SECRET_NAME.search(name) or SECRET_VALUE.search(value.strip()))


def private_temp(home: Path | None = None) -> Path:
    """One directory per MossCode process under ~/.mosscode/tmp, removed when the process exits."""
    global _temp
    if _temp is not None and _temp.is_dir():
        return _temp
    parent = (home or Path.home()) / ".mosscode" / "tmp"
    parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    for old in parent.iterdir():
        try:
            if old.is_dir() and now - old.stat().st_mtime > STALE_SECONDS:
                shutil.rmtree(old, ignore_errors=True)
        except OSError:
            pass
    _temp = parent / f"{os.getpid()}-{secrets.token_hex(4)}"
    _temp.mkdir()
    atexit.register(shutil.rmtree, _temp, ignore_errors=True)
    return _temp


def command_environment(env: Mapping[str, str] | None) -> dict[str, str]:
    """env (or this process's environment) as a model-run command should get it."""
    source = child_environment(env)
    if source is None:
        source = dict(os.environ)
    cleaned = {name: value for name, value in source.items() if not removed(name, value)}
    temp = str(private_temp())
    for name in list(cleaned):
        if name.upper() in TEMP_NAMES:
            del cleaned[name]  # Windows names ignore case; one spelling of each is enough.
    cleaned.update(dict.fromkeys(TEMP_NAMES, temp))
    return cleaned


def reset() -> None:
    """Tests only."""
    global _temp
    configure()
    _temp = None
