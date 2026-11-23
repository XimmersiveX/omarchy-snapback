"""Work out what a restore would do, without doing any of it.

A plan says, for each saved window, whether it is already where it belongs,
is open but somewhere else (move it), or isn't open (launch it), and which
workspaces have to be made or moved onto their monitors first. Nothing here
touches the desktop: it reads a session and a snapshot and returns a plan.

Where a window goes:
    * Its saved (monitor, slot) becomes today's workspace number through
      Omarchy's blocks file, so it lands on the same physical screen whatever
      numbers Omarchy hands out now.
    * If that monitor isn't plugged in, the window waits on another screen
      under its own monitor's workspace number (or the number Omarchy merged
      that workspace into), and Omarchy takes it home when the monitor comes
      back. A waiting floating window keeps its spot: Hyprland carries its
      offset back to the returning monitor unchanged.
    * Special workspaces keep their name. A session saved without Omarchy's
      blocks keeps plain workspace numbers.

Which open window is which saved one: the same app class, the same title
first, then the rest in focus order. One process can own several windows
(Chromium), so the process is never used. Windows the session doesn't know
are left alone.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable

from .apps import DesktopEntry
from .capture import CapturedWindow, Source, capture
from .geometry import Monitor, Placement, Rect, project
from .omarchy import SLOTS

GEOMETRY_TOLERANCE = 2.0


@dataclass
class WorkspaceStep:
    """A workspace that has to be on a monitor before windows go into it."""

    workspace: int
    monitor: str             # connector it must be on
    owner: str              # identity of the monitor it belongs to
    now_on: str | None       # connector it is on now; None if it doesn't exist
    waiting: bool = False    # its own monitor isn't plugged in; `monitor` hosts it

    @property
    def action(self) -> str:
        if self.now_on is None:
            return "create"
        return "ok" if self.now_on == self.monitor else "move"

    def to_dict(self) -> dict:
        return {"workspace": self.workspace, "monitor": self.monitor, "owner": self.owner,
                "now_on": self.now_on, "waiting": self.waiting, "action": self.action}


@dataclass
class WindowStep:
    app: str
    title: str
    action: str                       # launch, move, in-place, skip, problem
    workspace: str | None = None      # target: "13", "special:notes"
    monitor: str | None = None        # connector the workspace is on (or will be)
    owner: str | None = None          # identity of the monitor it belongs to
    slot: int | None = None
    waiting: bool = False             # owner monitor not plugged in
    floating: bool = False
    geometry: Rect | None = None      # floating windows only; None = leave as is
    command: list[str] | None = None  # launch only
    window: str | None = None         # address of the open window it matched
    now: str | None = None            # workspace that window is on now
    reason: str | None = None         # skip or problem
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        data = self.__dict__.copy()
        data["geometry"] = self.geometry.__dict__ if self.geometry else None
        return data


@dataclass
class Plan:
    session: str
    saved_at: str | None
    workspaces: list[WorkspaceStep]
    windows: list[WindowStep]
    missing_monitors: list[str]       # identities with windows but not plugged in
    untouched: int                    # open windows the session doesn't know

    def count(self, action: str) -> int:
        return sum(1 for w in self.windows if w.action == action)

    def to_dict(self) -> dict:
        return {
            "session": self.session,
            "saved_at": self.saved_at,
            "workspaces": [w.to_dict() for w in self.workspaces],
            "windows": [w.to_dict() for w in self.windows],
            "missing_monitors": self.missing_monitors,
            "untouched": self.untouched,
            "summary": {a: self.count(a) for a in ("launch", "move", "in-place", "skip", "problem")},
        }


def terminal_command(cwd: str | None) -> list[str]:
    command = ["uwsm-app", "--", "xdg-terminal-exec"]
    return command + [f"--dir={cwd}"] if cwd else command


def desktop_command(entry_id: str) -> list[str]:
    return ["uwsm-app", "--", entry_id]


def build(session: dict, source: Source, entries: dict[str, DesktopEntry],
          never: Callable[[str], bool] = lambda text: False,
          default_terminal: str | None = None,
          is_dir: Callable[[str], bool] = os.path.isdir,
          name: str = "") -> Plan:
    """The plan for restoring `session` onto the desktop in `source`.

    `entries` are the apps installed now, `never` is the never-restore check
    from the user's config as it is now, and `default_terminal` is the entry
    xdg-terminal-exec opens.
    """
    monitors = source.monitors()
    by_identity = {m.identity: m for m in monitors}
    by_connector = {m.name: m for m in monitors}
    state = source.workspaces
    host = _host(monitors, state.blocks)
    existing = _existing_workspaces(source)
    clients = {c["address"]: c for c in source.snapshot["clients"]}

    open_windows = capture(source)
    matches = _match(session["windows"], open_windows)

    workspace_steps: dict[int, WorkspaceStep] = {}
    missing: list[str] = []
    steps = []
    for index, saved in enumerate(session["windows"]):
        step = WindowStep(saved["app"], saved.get("title", ""), "problem",
                          floating=bool(saved.get("floating")))
        steps.append(step)
        reason = saved.get("skip")
        if reason or never(saved["app"]) or (saved.get("launch") and never(saved["launch"]["entry"])):
            step.action, step.reason = "skip", reason or f"never restore: {saved['app']}"
            continue

        target = _target(saved, state, by_identity, host, existing)
        if target is None:
            step.reason = "no workspace to put it on"
            continue
        step.workspace, step.monitor, step.owner, step.slot, step.waiting = target
        if step.waiting and step.owner not in missing:
            missing.append(step.owner)

        number = int(step.workspace) if step.workspace.lstrip("-").isdigit() else None
        if number is not None and number > 0 and step.monitor and number not in workspace_steps:
            workspace_steps[number] = WorkspaceStep(
                number, step.monitor, step.owner or "", existing.get(number), step.waiting)

        current = matches.get(index)
        placement = saved.get("placement")
        if step.floating and placement and not step.waiting:
            # Back on its own monitor or, for a special workspace or a session
            # without Omarchy, on whichever screen will show it.
            screen = by_connector.get(step.monitor) if step.monitor else None
            screen = screen or by_identity.get(saved.get("shown_on")) or host
            if screen:
                step.geometry = project(Placement.from_dict(placement), screen)
        if step.floating and step.waiting:
            step.notes.append("keeps its spot until its monitor is back")

        if current:
            step.window, step.now = current.address, current.workspace
            in_place = (current.workspace == step.workspace
                        and current.floating == step.floating
                        and (step.geometry is None
                             or _close(_rect(clients[current.address]), step.geometry)))
            step.action = "in-place" if in_place else "move"
            continue

        launch = saved.get("launch")
        if not launch:
            step.reason = saved.get("problem") or "Snapback doesn't know how to open it"
            continue
        entry = entries.get(launch["entry"])
        if entry is None:
            step.reason = f"{launch['entry']} is no longer installed"
            continue
        step.action = "launch"
        if launch["how"] == "terminal":
            cwd = launch.get("cwd")
            if cwd and not is_dir(cwd):
                step.notes.append(f"{cwd} no longer exists; opens in your home folder")
                cwd = None
            step.command = terminal_command(cwd)
            if default_terminal and default_terminal != launch["entry"]:
                step.notes.append(f"opens in your default terminal ({default_terminal}), "
                                  f"not {launch['entry']}")
        else:
            step.command = desktop_command(entry.id)

    untouched = len(open_windows) - sum(1 for s in steps if s.window)
    return Plan(name, session.get("saved_at"), sorted(workspace_steps.values(), key=lambda w: w.workspace),
                steps, missing, untouched)


def _host(monitors: list[Monitor], blocks: dict[str, int]) -> Monitor | None:
    """Where the windows of an unplugged monitor wait: the plugged-in monitor
    with the lowest Omarchy block, as Omarchy itself would merge them."""
    if not monitors:
        return None
    return min(monitors, key=lambda m: (blocks.get(m.identity, 1 << 30), m.name))


def _existing_workspaces(source: Source) -> dict[int, str]:
    """Regular workspace id -> connector of the monitor it is on."""
    return {w["id"]: w["monitor"] for w in source.snapshot.get("workspaces", []) if w["id"] > 0}


def _target(saved: dict, state, by_identity: dict[str, Monitor], host: Monitor | None,
            existing: dict[int, str]) -> tuple[str, str | None, str | None, int | None, bool] | None:
    """(workspace, connector, owner identity, slot, waiting) for a saved window."""
    workspace = str(saved.get("workspace", ""))
    owner, slot = saved.get("monitor"), saved.get("slot")

    if not owner or not slot:
        if workspace.startswith("special:"):
            return workspace, None, None, None, False
        if workspace.isdigit():  # saved without Omarchy's blocks
            return workspace, existing.get(int(workspace)), None, None, False
        return None

    monitor = by_identity.get(owner)
    if monitor:
        number = state.workspace_for(owner, slot)
        if number is None:  # plugged in, but Omarchy hasn't given it a block
            return None
        return str(number), monitor.name, owner, slot, False

    # Not plugged in. If Omarchy already merged this slot elsewhere, join that
    # workspace: making the original number now would make Omarchy renumber the
    # merged one when the monitor returns. Only while it exists, though: a
    # record outlives its workspace, and Omarchy forgets the record as soon as
    # that number is created again, which would strand the windows.
    for current, (original, identity) in sorted(state.merged.items()):
        if identity == owner and (original - 1) % SLOTS + 1 == slot and current in existing:
            return str(current), existing.get(current) or (host.name if host else None), owner, slot, True
    number = state.workspace_for(owner, slot)
    if number is None:
        return None
    return str(number), existing.get(number) or (host.name if host else None), owner, slot, True


def _match(saved: list[dict], open_windows: list[CapturedWindow]) -> dict[int, CapturedWindow | None]:
    """Saved window index -> the open window that is it, if any."""
    pool: dict[str, list[CapturedWindow]] = {}
    for window in sorted(open_windows, key=lambda w: (w.focus < 0, w.focus)):
        pool.setdefault(window.app, []).append(window)

    result: dict[int, CapturedWindow | None] = {}
    for exact in (True, False):
        for index, s in enumerate(saved):
            if index in result or s.get("skip"):
                continue
            candidates = pool.get(s["app"], [])
            for window in candidates:
                if not exact or window.title == s.get("title"):
                    result[index] = window
                    candidates.remove(window)
                    break
    return result


def _rect(client: dict) -> Rect:
    return Rect(*client["at"], *client["size"])


def _close(a: Rect, b: Rect) -> bool:
    return all(abs(p - q) <= GEOMETRY_TOLERANCE for p, q in ((a.x, b.x), (a.y, b.y), (a.w, b.w), (a.h, b.h)))
