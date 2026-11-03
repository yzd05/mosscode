"""Per-project storage under ~/.mosscode, shared by session transcripts and memory notes."""

import hashlib
import os
import re
import secrets
from datetime import datetime
from pathlib import Path

SESSION_ID = re.compile(r"\d{8}-\d{6}-[0-9a-f]{4}")


def mosscode_home(home: Path | None = None) -> Path:
    return (Path.home() if home is None else home) / ".mosscode"


def project_key(root: Path) -> str:
    """Readable tail of the resolved path plus a hash, so a-b and a/b never share a key."""
    path = str(Path(root).resolve())
    if os.name == "nt":
        path = path.casefold()
    readable = re.sub(r"[^A-Za-z0-9]", "-", path)[-48:]
    return f"{readable}-{hashlib.sha256(path.encode('utf-8')).hexdigest()[:8]}"


def project_dir(root: Path, home: Path | None = None) -> Path:
    return mosscode_home(home) / "projects" / project_key(root)


def new_session_id(now: datetime | None = None) -> str:
    return f"{(now or datetime.now()).strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"
