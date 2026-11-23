"""Keep the session saved while you work, and never save the teardown.

The watcher listens to Hyprland's event socket and saves the "last" session
a few seconds after windows open, close, move or change, and once a minute
anyway (Hyprland sends no event when a floating window is moved or resized).

What it never saves:
    * the desktop closing down: Omarchy's log out, reboot and shut down close
      every window before acting (omarchy-system ... --execute), so a save is
      skipped while that runs, while `uwsm stop` runs, and once logind is
      preparing to shut down. A burst of closing windows (three within two
      seconds) also holds saving off until things have been calm for a while,
      for exits that go some other way.
    * a session that reopens nothing over one that reopens something (the
      guard in session.write).
    * anything while a restore runs, which would record a half-restored desk.

It saves at most every few seconds however busy the titles are, and only
rewrites the file when something in it changed.
"""

from __future__ import annotations

import fcntl
import json
import os
import select
import socket
import subprocess
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import restore, session

# High-frequency events that say nothing about what is open or where.
IGNORED = {"activewindow", "activewindowv2", "focusedmon", "focusedmonv2", "workspace",
           "workspacev2", "activespecial", "activespecialv2", "activelayout", "submap",
           "bell", "screencast", "screencastv2", "configreloaded", "minimized"}

EOF_EVENT = object()


@dataclass
class Timing:
    debounce: float = 3.0         # quiet time after a change before saving
    max_wait: float = 15.0        # but save within this long while changes keep coming
    periodic: float = 60.0        # save this often anyway
    min_interval: float = 10.0    # never save more often than this
    burst_window: float = 2.0     # this many seconds ...
    burst_closes: int = 3         # ... with this many windows closing is a burst
    burst_calm: float = 20.0      # after a burst, wait this long without one


@dataclass
class Watcher:
    save: Callable[[], bool]            # saves now; True if the file changed
    may_save: Callable[[], str | None]  # None, or why saving is off right now
    clock: Callable[[], float] = time.monotonic
    timing: Timing = field(default_factory=Timing)
    log: Callable[[str], None] = lambda line: None

    def run(self, next_event: Callable[[float], object]) -> None:
        """`next_event(timeout)` returns an event line, None on timeout, or
        EOF_EVENT when Hyprland has gone."""
        t = self.timing
        now = self.clock()
        dirty = True            # save once at the start
        dirty_since = changed_at = now - t.debounce
        hold_until = now
        last_save = now - t.min_interval
        next_periodic = now + t.periodic
        closes: deque[float] = deque()

        def due() -> float:
            # Quiet for `debounce`, but no longer than `max_wait` while events
            # keep coming; never inside a hold or sooner than min_interval.
            settled = min(changed_at + t.debounce, dirty_since + t.max_wait)
            return max(settled, hold_until, last_save + t.min_interval)

        while True:
            wake = min(due(), next_periodic) if dirty else next_periodic
            event = next_event(max(0.0, wake - self.clock()))
            if event is EOF_EVENT:
                self.log("Hyprland has gone; stopping")
                return
            now = self.clock()

            name = event.split(">>", 1)[0] if isinstance(event, str) else ""
            if name and name not in IGNORED:
                if not dirty:
                    dirty_since = now
                dirty, changed_at = True, now
                if name == "closewindow":
                    closes.append(now)
                    while closes and closes[0] < now - t.burst_window:
                        closes.popleft()
                    if len(closes) >= t.burst_closes:
                        hold_until = now + t.burst_calm
                        self.log("windows are closing fast; holding saves")

            if not ((dirty and now >= due()) or now >= next_periodic):
                continue
            next_periodic = now + t.periodic
            reason = self.may_save()
            if reason:
                self.log(f"not saving: {reason}")
                hold_until = now + t.debounce
                continue
            try:
                if self.save():
                    self.log("saved")
            except session.SessionError as exc:
                self.log(f"not saving: {exc}")
            dirty, last_save = False, self.clock()


def shutdown_in_progress(proc: Path = Path("/proc")) -> str | None:
    """Why the session is ending, if it is: Omarchy's exit, uwsm stop, or logind."""
    for entry in proc.iterdir() if proc.is_dir() else ():
        if not entry.name.isdigit():
            continue
        try:
            args = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        words = [os.path.basename(a.decode(errors="replace")) for a in args if a]
        if "omarchy-hyprland-window-close-all" in words:
            return "Omarchy is closing every window"
        if "omarchy-system" in words and ("--execute" in words or "--inhibited" in words):
            return "Omarchy is logging out or shutting down"
        if "uwsm" in words and "stop" in words:
            return "the session is stopping"
    try:
        result = subprocess.run(
            ["busctl", "--system", "get-property", "org.freedesktop.login1", "/org/freedesktop/login1",
             "org.freedesktop.login1.Manager", "PreparingForShutdown"],
            capture_output=True, text=True, timeout=3)
        if result.stdout.strip() == "b true":
            return "the system is shutting down"
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def restore_running(path: Path | None = None) -> bool:
    """True while a restore holds its lock."""
    path = path or restore.lock_path()
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


def comparable(saved: dict) -> str:
    """A session minus the time it was saved, to tell whether anything changed."""
    return json.dumps({k: v for k, v in saved.items() if k != "saved_at"}, sort_keys=True)


def events_from(sock: socket.socket) -> Callable[[float], object]:
    """next_event(timeout) over Hyprland's event socket."""
    buffer = b""
    lines: deque[str] = deque()

    def next_event(timeout: float) -> object:
        nonlocal buffer
        if lines:
            return lines.popleft()
        ready, _, _ = select.select([sock], [], [], timeout)
        if not ready:
            return None
        chunk = sock.recv(65536)
        if not chunk:
            return EOF_EVENT
        buffer += chunk
        *complete, buffer = buffer.split(b"\n")
        lines.extend(line.decode(errors="replace") for line in complete if line)
        return lines.popleft() if lines else ""

    return next_event


def event_socket() -> socket.socket:
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    signature = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
    if not signature:
        raise session.SessionError("HYPRLAND_INSTANCE_SIGNATURE isn't set; is Hyprland running?")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(str(Path(runtime) / "hypr" / signature / ".socket2.sock"))
    return sock


class WatchLock:
    """One watcher at a time."""

    def __init__(self, path: Path | None = None):
        runtime = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
        self.path = path or Path(runtime) / "snapback-watch.lock"
        self.file = None

    def __enter__(self) -> "WatchLock":
        self.file = open(self.path, "w")
        try:
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.file.close()
            raise session.SessionError("Snapback is already watching this session") from exc
        return self

    def __exit__(self, *exc) -> None:
        if self.file:
            self.file.close()
