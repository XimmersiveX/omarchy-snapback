"""Saved sessions: what was open, where, and how to open it again.

A session is a JSON file under ~/.local/state/snapback/sessions/, readable by
the user only. Windows are recorded against the physical monitor that owns
their workspace and its slot (1-10), not the global workspace number, so a
restore lands them on the right screen whatever numbers Omarchy hands out.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from .apps import ProcInfo, Resolver
from .capture import CapturedWindow, Source, capture
from .screens import pick_main

SCHEMA = 1
DEFAULT_NAME = "last"
UNDO_NAME = "before-switch"  # the desk as it was before the last switch
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")


class SessionError(RuntimeError):
    pass


def sessions_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local/state")
    return Path(base) / "snapback" / "sessions"


def session_path(name: str, directory: Path | None = None) -> Path:
    if not NAME_RE.match(name) or name.endswith(".json"):
        raise SessionError(f"bad session name {name!r}: use letters, digits, '.', '_' or '-'")
    return (directory or sessions_dir()) / f"{name}.json"


@dataclass
class SavedWindow:
    window: CapturedWindow
    launch: dict | None
    skip: str | None
    problem: str | None
    tabs: dict | None = None   # a browser window's {"profile", "urls"}

    def to_dict(self) -> dict:
        w = self.window
        return {
            "app": w.app,
            "title": w.title,
            "workspace": w.workspace,
            "monitor": w.owner,
            "slot": w.slot,
            "shown_on": w.shown_on,
            "floating": w.floating,
            "fullscreen": w.fullscreen,
            "pinned": w.pinned,
            "grouped": w.grouped,
            "focus": w.focus,
            "stable_id": w.stable_id,
            "placement": w.placement.to_dict() if w.placement else None,
            "launch": self.launch,
            "skip": self.skip,
            "problem": self.problem,
            "tabs": self.tabs,
        }


def build(source: Source, resolver: Resolver, procs: Callable[[int], ProcInfo],
          versions: dict | None = None, now: datetime | None = None,
          marks: set[str] | frozenset = frozenset(),
          tabs: Callable[[list[dict]], dict[str, dict]] = lambda clients: {}) -> dict:
    """The session for a snapshot. `procs` looks up a window's process (live
    /proc, or a stand-in in tests); `tabs` reads browser windows' tabs
    (browsers.open_tabs, or a stand-in)."""
    saved = []
    browser_tabs = tabs(source.snapshot["clients"])
    for window in capture(source):
        result = resolver.resolve(window.app, procs(window.pid))
        saved.append(SavedWindow(
            window,
            result.launch.to_dict() if result.launch else None,
            result.skip,
            result.problem,
            browser_tabs.get(window.address) if (browser_tabs.get(window.address) or {}).get("urls") else None,
        ))
    saved.sort(key=lambda s: (s.window.focus < 0, s.window.focus))

    active = {m["name"]: (m.get("activeWorkspace") or {}).get("id")
              for m in source.snapshot["monitors"]}
    main = pick_main([(m.identity, m.box) for m in source.monitors()], set(marks))
    monitors = []
    for m in source.monitors():
        monitors.append({
            "identity": m.identity,
            "connector": m.name,
            "box": [m.box.x, m.box.y, m.box.w, m.box.h],
            "scale": m.scale,
            "transform": m.transform,
            "block": source.workspaces.blocks.get(m.identity),
            "view": view(source, m.identity, active.get(m.name)),
            "main": m.identity == main,
        })

    return {
        "snapback": SCHEMA,
        "saved_at": (now or datetime.now().astimezone()).isoformat(timespec="seconds"),
        "versions": versions or {},
        "hyprland_instance": source.instance,
        "monitors": monitors,
        "windows": [s.to_dict() for s in saved],
    }


def view(source: Source, identity: str, workspace_id: int | None) -> dict | None:
    """The workspace a screen shows, as its slot when it is one of the screen's
    own (Omarchy), else as its number."""
    if not workspace_id or workspace_id < 1:
        return None
    owner = source.workspaces.owner(workspace_id)
    if owner and owner[0] == identity:
        return {"slot": owner[1]}
    if not source.workspaces.found:
        return {"workspace": str(workspace_id)}
    return None  # showing another screen's workspace (merged while it is unplugged)


def restorable(session: dict) -> int:
    return sum(1 for w in session.get("windows", []) if w.get("launch"))


def write(session: dict, path: Path, force: bool = False) -> None:
    """Write atomically, readable only by the user.

    Refuses to replace a session that would reopen something with one that
    would reopen nothing, which is what saving during logout, with windows
    already closing, would otherwise do. `force` overrides that.
    """
    if not force and restorable(session) == 0 and path.exists():
        try:
            previous = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            previous = {}
        if restorable(previous) > 0:
            raise SessionError(
                f"nothing to reopen in this session, so {path} (which has "
                f"{restorable(previous)}) was kept; use --force to replace it")

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = json.dumps(session, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as file:  # mkstemp creates it 0600
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def read(path: Path) -> dict:
    try:
        session = json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise SessionError(f"no saved session at {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise SessionError(f"can't read {path}: {exc}") from exc
    if not isinstance(session, dict):
        raise SessionError(f"{path} isn't a Snapback session")
    if session.get("snapback") != SCHEMA:
        raise SessionError(f"{path} is from a different Snapback version "
                           f"(format {session.get('snapback')!r}, expected {SCHEMA})")
    problem = _shape_problem(session)
    if problem:
        raise SessionError(f"{path} is damaged: {problem}")
    return session


def _shape_problem(session: dict) -> str | None:
    """What the code that reads a session relies on, checked once here."""
    windows, monitors = session.get("windows", []), session.get("monitors", [])
    if not isinstance(windows, list) or not isinstance(monitors, list):
        return "windows and monitors must be lists"
    if not all(isinstance(m, dict) for m in monitors):
        return "a monitor isn't an object"
    for w in windows:
        if not isinstance(w, dict) or not isinstance(w.get("app"), str):
            return "a window has no app"
        if not isinstance(w.get("slot"), (int, type(None))) or isinstance(w.get("slot"), bool):
            return f"{w['app']}: slot must be a number"
        for key in ("launch", "tabs"):
            if not isinstance(w.get(key), (dict, type(None))):
                return f"{w['app']}: {key} must be an object"
        launch = w.get("launch")
        if launch and not (isinstance(launch.get("entry"), str) and isinstance(launch.get("how"), str)):
            return f"{w['app']}: launch needs an entry and how"
    return None
