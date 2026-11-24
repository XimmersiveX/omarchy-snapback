"""What the panel shows: is Snapback on, what's saved, how the last restore went.

`snapback status --json` prints one snapshot of this, so the Omarchy panel
stays a thin view over the command line. Restores record their outcome in
~/.local/state/snapback/last-restore.json, because a restore at login runs
long before anyone opens the panel.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from . import files, session
from .apps import DesktopEntry

# The sessions Snapback keeps itself, listed first, in this order.
OWN_SESSIONS = (session.DEFAULT_NAME, session.UNDO_NAME, "previous")
OUTCOMES = ("placed", "in-place", "failed", "skipped", "unrestorable")


def report_path() -> Path:
    return files.state_dir() / "last-restore.json"


def restore_record(name: str, trigger: str, report: dict | None = None,
                   error: str | None = None, check: list | None = None,
                   now: datetime | None = None) -> dict:
    """What a restore did, for the panel. `trigger` is "login" or "manual";
    `error` is set when the restore couldn't run at all."""
    results = (report or {}).get("results", [])
    counts = {outcome: sum(1 for r in results if r.get("outcome") == outcome)
              for outcome in OUTCOMES}
    problems = [{"app": r.get("app"), "title": r.get("title"), "detail": r.get("detail")}
                for r in results if r.get("outcome") in ("failed", "unrestorable")]
    errors = list((report or {}).get("errors", []))
    if error:
        errors.insert(0, error)
    return {
        "at": (now or datetime.now().astimezone()).isoformat(timespec="seconds"),
        "session": name,
        "trigger": trigger,
        "ok": not error and not errors and not problems and not check,
        "counts": counts,
        "problems": problems,
        "errors": errors,
        "misplaced": len(check or []),
        "closed": len((report or {}).get("closed", [])),
        "kept": list((report or {}).get("kept", [])),
        "report": report,
    }


def record_restore(record: dict, path: Path | None = None) -> None:
    try:
        files.write_json(record, path or report_path())
    except OSError:
        pass  # the restore itself happened; a missing report is no reason to fail it


def last_restore(path: Path | None = None) -> dict | None:
    try:
        data = json.loads((path or report_path()).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def app_name(window: dict, entries: dict[str, DesktopEntry]) -> str:
    launch = window.get("launch") or {}
    entry = entries.get(launch.get("entry") or "")
    return entry.name if entry else window.get("app") or "?"


def describe_session(path: Path, entries: dict[str, DesktopEntry]) -> dict:
    name = path.stem
    try:
        saved = session.read(path)
    except session.SessionError as exc:
        return {"name": name, "error": str(exc)}
    windows = saved.get("windows", [])
    apps: list[str] = []
    for w in windows:
        if w.get("skip"):
            continue
        label = app_name(w, entries)
        if label not in apps:
            apps.append(label)
    return {
        "name": name,
        "saved_at": saved.get("saved_at"),
        "windows": sum(1 for w in windows if not w.get("skip")),
        "reopen": session.restorable(saved),
        "attention": sum(1 for w in windows if w.get("problem")),
        "monitors": len(saved.get("monitors", [])),
        "apps": apps,
    }


def list_sessions(entries: dict[str, DesktopEntry], directory: Path | None = None) -> list[dict]:
    """Snapback's own sessions first (last, before-switch, previous), then named ones, newest first."""
    directory = directory or session.sessions_dir()
    try:
        paths = [p for p in directory.glob("*.json") if session.NAME_RE.match(p.stem)]
    except OSError:
        return []
    found = [describe_session(p, entries) for p in paths]
    own = [s for name in OWN_SESSIONS for s in found if s["name"] == name]
    named = sorted((s for s in found if s["name"] not in OWN_SESSIONS),
                   key=lambda s: (s.get("saved_at") or "", s["name"]), reverse=True)
    return own + named


def hook_state(path: Path, marker: str) -> str:
    """"on" (our hook is there), "off", or "foreign" (a file of that name we didn't write)."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        return "off"
    except OSError:
        return "foreign"
    return "on" if marker in text else "foreign"


def snapshot(*, hook: str, hook_path: Path, watching: bool, restoring: bool, version: str,
             entries: dict[str, DesktopEntry], directory: Path | None = None,
             report: Path | None = None, screens: list[dict] | None = None,
             fold: str = "omarchy") -> dict:
    return {
        "version": version,
        "atLogin": hook,
        "hookPath": str(hook_path),
        "watching": watching,
        "restoring": restoring,
        "sessions": list_sessions(entries, directory),
        "lastRestore": last_restore(report),
        "screens": screens or [],
        "fold": fold,
    }
