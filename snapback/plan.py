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

Which open window is which saved one, always within the same app class:
    1. In the Hyprland session the save was made in, the very same window
       (Hyprland's stableId, which survives title changes and is never reused).
    2. The same title.
    3. Any other open window of the app, preferring one already on the
       saved window's workspace, then the most recently used. Titles change
       all the time (a browser's is its tab, T3 Code's its thread), and an app
       that keeps one window, like T3 Code, can't be opened a second time, so
       an open window of the app is used before another is launched.
One process can own several windows (Chromium), so the process is never used.
Windows the session doesn't name are left alone, or listed in `others` for a
restore that closes them ("switch to" a session).

Each screen goes back to the workspace it was showing when the session was
saved (`views`), where the session recorded it and the screen is plugged in.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable

from .apps import TUI_ID, DesktopEntry, exec_args
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
    tile: tuple[float, float] | None = None  # tiled: saved center as a fraction of the monitor
    tile_box: tuple[float, float, float, float] | None = None  # tiled: saved (x0, y0, x1, y1), same
    command: list[str] | None = None  # launch only
    window: str | None = None         # address of the open window it matched
    matched_by: str | None = None     # window (same window), title, or app (any of its windows)
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
    others: list[dict] = field(default_factory=list)  # those on regular workspaces: what
                                                      # switching to the session closes
    views: dict[str, str] = field(default_factory=dict)  # connector -> workspace it showed

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
            "others": self.others,
            "views": self.views,
            "summary": {a: self.count(a) for a in ("launch", "move", "in-place", "skip", "problem")},
        }


def terminal_command(cwd: str | None) -> list[str]:
    command = ["uwsm-app", "--", "xdg-terminal-exec"]
    return command + [f"--dir={cwd}"] if cwd else command


def desktop_command(entry_id: str) -> list[str]:
    return ["uwsm-app", "--", entry_id]


def tui_command(app_id: str, entry: DesktopEntry) -> list[str]:
    """How omarchy-launch-tui opens a terminal app, from the app's own entry."""
    return ["uwsm-app", "--", "xdg-terminal-exec", f"--app-id={app_id}", "-e", *exec_args(entry.exec)]


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
    same_instance = bool(source.instance) and session.get("hyprland_instance") == source.instance
    targets = [_target(saved, state, by_identity, host, existing) for saved in session["windows"]]
    matches = _match(session["windows"], open_windows, same_instance,
                     [t[0] if t else None for t in targets])

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

        target = targets[index]
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

        match = matches.get(index)
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
        if not step.floating and placement:
            p = Placement.from_dict(placement)
            step.tile = ((p.dx + p.w / 2) / p.monitor_w, (p.dy + p.h / 2) / p.monitor_h)
            step.tile_box = (p.dx / p.monitor_w, p.dy / p.monitor_h,
                             (p.dx + p.w) / p.monitor_w, (p.dy + p.h) / p.monitor_h)

        if match:
            current, step.matched_by = match
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
        if launch["how"] == "tui":
            app_id = launch.get("app_id") or ""
            if not TUI_ID.match(app_id) or not entry.terminal or not exec_args(entry.exec):
                step.action, step.reason = "problem", f"can't open {entry.id} in a terminal"
                continue
            step.command = tui_command(app_id, entry)
        elif launch["how"] == "terminal":
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

    taken = {s.window for s in steps if s.window}
    others = [{"address": w.address, "app": w.app, "name": app_name(w.app, entries), "title": w.title,
               "workspace": w.workspace}
              for w in sorted(open_windows, key=lambda w: (w.focus < 0, w.focus))
              if w.address not in taken and not w.workspace.startswith("special:")]
    return Plan(name, session.get("saved_at"), sorted(workspace_steps.values(), key=lambda w: w.workspace),
                steps, missing, len(open_windows) - len(taken), others,
                _views(session, state, by_identity))


def app_name(app: str, entries: dict[str, DesktopEntry]) -> str:
    """An app's name from its desktop entry, for people; its class otherwise."""
    for entry in entries.values():
        if entry.wm_class == app:
            return entry.name
    entry = entries.get(f"{app}.desktop") or entries.get(f"{app.lower()}.desktop")
    return entry.name if entry else app


def _views(session: dict, state, by_identity: dict[str, Monitor]) -> dict[str, str]:
    """Connector -> the workspace that screen showed when the session was saved."""
    views = {}
    for saved in session.get("monitors", []):
        monitor = by_identity.get(saved.get("identity"))
        view = saved.get("view") or {}
        if not monitor:
            continue
        if view.get("slot"):
            number = state.workspace_for(monitor.identity, view["slot"])
            if number is not None:
                views[monitor.name] = str(number)
        elif str(view.get("workspace", "")).isdigit():  # saved without Omarchy's blocks
            views[monitor.name] = str(view["workspace"])
    return views


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


def _match(saved: list[dict], open_windows: list[CapturedWindow], same_instance: bool,
           targets: list[str | None]) -> dict[int, tuple[CapturedWindow, str]]:
    """Saved window index -> (the open window that is it, what matched).
    `targets[i]` is the workspace saved window i goes to."""
    pool: dict[str, list[CapturedWindow]] = {}
    for window in sorted(open_windows, key=lambda w: (w.focus < 0, w.focus)):
        pool.setdefault(window.app, []).append(window)

    tests = []
    if same_instance:
        tests.append(("window", lambda w, s: bool(w.stable_id) and w.stable_id == s.get("stable_id")))
    tests.append(("title", lambda w, s: w.title == s.get("title")))
    tests.append(("app", lambda w, s: True))

    result: dict[int, tuple[CapturedWindow, str]] = {}
    for how, test in tests:
        for index, s in enumerate(saved):
            if index in result or s.get("skip"):
                continue
            candidates = pool.get(s["app"], [])
            ordered = candidates
            if how == "app":  # one already where it goes moves nothing
                ordered = sorted(candidates, key=lambda w: w.workspace != targets[index])
            for window in ordered:
                if test(window, s):
                    result[index] = (window, how)
                    candidates.remove(window)
                    break
    return result


def _rect(client: dict) -> Rect:
    return Rect(*client["at"], *client["size"])


def _close(a: Rect, b: Rect) -> bool:
    return all(abs(p - q) <= GEOMETRY_TOLERANCE for p, q in ((a.x, b.x), (a.y, b.y), (a.w, b.w), (a.h, b.h)))
