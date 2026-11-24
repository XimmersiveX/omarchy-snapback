"""Where Snapback keeps things, how it writes them, and its one-at-a-time locks."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from pathlib import Path


def state_home() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")


def config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def runtime_dir() -> Path:
    return Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")


def state_dir() -> Path:
    """~/.local/state/snapback: setups, the last restore, marked screens."""
    return state_home() / "snapback"


def write_json(data: object, path: Path, sort_keys: bool = False) -> None:
    """Write atomically (a reader sees the old file or the new one, never half),
    readable only by the user."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as file:  # mkstemp creates it 0600
            file.write(json.dumps(data, indent=2, sort_keys=sort_keys) + "\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


class Lock:
    """Held while one Snapback process does something only one may do at a
    time; another trying it at once gets `busy` raised instead of waiting."""

    def __init__(self, path: Path, busy: Exception):
        self.path, self.busy, self.file = path, busy, None

    def __enter__(self) -> "Lock":
        self.file = open(self.path, "w")
        try:
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.file.close()
            raise self.busy from exc
        return self

    def __exit__(self, *exc) -> None:
        if self.file:
            self.file.close()  # closing releases the lock


def held(path: Path) -> bool:
    """Whether another process holds the lock at `path` right now."""
    try:
        with open(path, "a") as file:
            try:
                fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(file, fcntl.LOCK_UN)
    except OSError:
        return False
    return False
