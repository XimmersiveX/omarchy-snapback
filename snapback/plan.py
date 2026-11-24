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
    2. For a browser window whose tabs were saved, an open window holding
       them (see below).
    3. The same title.
    4. Any other open window of the app, preferring one already on the
       saved window's workspace, then the most recently used. Titles change
       all the time (a browser's is its tab, T3 Code's its thread), and an app
       that keeps one window, like T3 Code, can't be opened a second time, so
       an open window of the app is used before another is launched.
One process can own several windows (Chromium), so the process is never used.
Windows the session doesn't name are left alone, or listed in `others` for a
restore that closes them ("switch to" a session).

A browser window whose tabs were saved is its tabs: an open window of the
browser is only taken for it when the open window's tabs are known and hold
every saved tab's site. Otherwise a window is opened with the saved tabs (see
browsers.py). Where the tabs aren't known on either side, as before.

Apps never restored (never_restore, Zoom by default) are never closed by a
switch either: the setup can't bring them back, and it would end a call.

Pinned windows are left alone: never matched, moved or closed. While an app
has a pinned window, its other windows aren't closed either (`pinned` lists
them): a video call's picture-in-picture is pinned, the browser window
holding the call isn't, and nothing links the two.

Each screen goes back to the workspace it was showing when the session was
saved (`views`), where the session recorded it and the screen is plugged in.

Away from the screens a setup was saved on, its screens are matched by role
and the leftovers fold into the main screen (see screens.py).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable

from . import browsers
from .apps import TUI_ID, DesktopEntry, exec_args
from .capture import CapturedWindow, Source, capture
from .geometry import Monitor, Placement, Rect, project
from .omarchy import MODES, SLOTS, slot_of
from .screens import FOLD_OMARCHY, Mapping, map_screens

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
    fullscreen: int = 0               # 0, 1 full width (maximized) or 2 full screen, as saved
    geometry: Rect | None = None      # floating windows only; None = leave as is
    tile: tuple[float, float] | None = None  # tiled: saved center as a fraction of the monitor
    tile_box: tuple[float, float, float, float] | None = None  # tiled: saved (x0, y0, x1, y1), same
    command: list[str] | None = None  # launch only
    tabs: list[str] | None = None     # launch only: the browser window opens with these tabs
    workspace_mode: str | None = None  # the Omarchy mode its workspace was saved in
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
    pinned: list[dict] = field(default_factory=list)  # left open by a switch: pinned, or an
                                                      # app with a pinned window
    views: dict[str, str] = field(default_factory=dict)  # connector -> workspace it showed
    screens: list[dict] = field(default_factory=list)  # how each saved screen lands today
    elsewhere: bool = False           # the setup's main screen isn't here: screens matched by role
    park: bool = False                # park windows before moving them (Snapback's own folding)
    modes: dict[str, str] = field(default_factory=dict)  # workspace -> Omarchy mode to set first
    warm: dict[str, list[str]] = field(default_factory=dict)  # closed browser -> command starting it,
                                                              # before its tab windows (switching)

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
            "pinned": self.pinned,
            "views": self.views,
            "screens": self.screens,
            "elsewhere": self.elsewhere,
            "modes": self.modes,
            "warm": self.warm,
            "summary": {a: self.count(a) for a in ("launch", "move", "in-place", "skip", "problem")},
        }


FULLSCREEN_NOTES = {1: "full width", 2: "full screen"}


def fullscreen_mode(saved: dict) -> int:
    """How a saved window filled its screen: 0 not, 1 full width (Hyprland's
    maximized: the workspace's area, bar still shown), 2 full screen."""
    mode = saved.get("fullscreen")
    return mode if mode in FULLSCREEN_NOTES and not isinstance(mode, bool) else 0


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
          name: str = "", marks: set[str] | frozenset = frozenset(),
          fold: str = FOLD_OMARCHY, tabs: dict[str, dict] | None = None,
          cold: Callable[[str, str], bool] = lambda app, profile: False,
          mode_now: Callable[[str], str] | None = None, switching: bool = False) -> Plan:
    """The plan for restoring `session` onto the desktop in `source`.

    `entries` are the apps installed now, `never` is the never-restore check
    from the user's config as it is now, and `default_terminal` is the entry
    xdg-terminal-exec opens. `tabs` are the open browser windows' tabs
    (browsers.open_tabs), and `cold(app, profile)` says whether opening a
    window of that browser now would bring back its own last session instead
    (browsers.cold_restore).
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
    follow = fold == FOLD_OMARCHY
    omarchy_folded = {(identity, slot_of(original))
                      for current, (original, identity) in state.merged.items()
                      if current in existing} if follow else set()
    # Workspaces Snapback's own folding passes over: Omarchy's folded ones, and
    # ones holding windows of apps the setup doesn't have (they stay).
    setup_apps = {w["app"] for w in session["windows"]}
    busy = {current for current in state.merged if current in existing}
    busy |= {int(w.workspace) for w in open_windows if w.workspace.isdecimal() and w.app not in setup_apps}
    by_block = {block: identity for identity, block in state.blocks.items()}
    in_use = {(by_block[(n - 1) // SLOTS], slot_of(n)) for n in busy
              if (n - 1) // SLOTS in by_block} if follow else set()
    mapping = map_screens(session, monitors, set(marks), omarchy_folded, in_use)
    targets = [_target(saved, state, by_identity, host, existing, mapping, follow)
               for saved in session["windows"]]
    tabs = tabs or {}
    matches = _match(session["windows"], [w for w in open_windows if not w.pinned], same_instance,
                     [t[0] if t else None for t in targets], tabs)

    workspace_steps: dict[int, WorkspaceStep] = {}
    warm: dict[str, list[str]] = {}
    missing: list[str] = []
    steps = []
    for index, saved in enumerate(session["windows"]):
        step = WindowStep(saved["app"], saved.get("title", ""), "problem",
                          floating=bool(saved.get("floating")), fullscreen=fullscreen_mode(saved),
                          workspace_mode=saved.get("workspace_mode") if saved.get("workspace_mode") in MODES
                          else None)
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

        number = int(step.workspace) if step.workspace.lstrip("-").isdecimal() else None
        if number is not None and number > 0 and step.monitor and number not in workspace_steps:
            workspace_steps[number] = WorkspaceStep(
                number, step.monitor, step.owner or "", existing.get(number), step.waiting)

        match = matches.get(index)
        # A full screen window was saved with the screen's box, not its own:
        # it keeps whatever size and tile it gets.
        placement = None if step.fullscreen else saved.get("placement")
        if step.fullscreen:
            step.notes.append(FULLSCREEN_NOTES[step.fullscreen] if not step.waiting
                              else f"not made {FULLSCREEN_NOTES[step.fullscreen]} while its monitor is away")
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
                        and (step.waiting or current.fullscreen == step.fullscreen)
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
            if not entry.terminal_emulator:
                step.action, step.reason = "problem", f"{entry.id} isn't a terminal"
                continue
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
            saved_tabs = saved.get("tabs") or {}
            urls = [u for u in saved_tabs.get("urls") or [] if browsers.usable(u)]
            profile = str(saved_tabs.get("profile") or "Default")
            is_cold = bool(urls) and cold(saved["app"], profile)
            if is_cold and not switching:
                step.notes.append("the browser isn't open and continues where it left off, "
                                  "so it brings back its own tabs")
            elif urls and browsers.is_browser(saved["app"], entry.id):
                command = browsers.new_window(exec_args(entry.exec), profile, urls)
                if command:
                    step.command, step.tabs = command, urls
                    step.notes.append(f"opens with its {len(urls)} tab" + ("s" if len(urls) > 1 else ""))
                    if is_cold:
                        warm.setdefault(saved["app"], desktop_command(entry.id))
                        step.notes.append("the browser isn't open: it is started first, out of sight, "
                                          "and the windows it brings back by itself are closed")

    taken = {s.window for s in steps if s.window}
    pinned_apps = {w.app for w in open_windows if w.pinned}
    skipped_apps = {s["app"] for s in session["windows"] if s.get("skip")}
    others, pinned = [], []
    for w in sorted(open_windows, key=lambda w: (w.focus < 0, w.focus)):
        if w.address in taken or w.workspace.startswith("special:"):
            continue
        if never(w.app) or w.app in skipped_apps:
            continue  # never reopened, so never closed either: a switch doesn't end a call
        (pinned if w.app in pinned_apps else others).append(
            {"address": w.address, "app": w.app, "name": app_name(w.app, entries), "title": w.title,
             "workspace": w.workspace})
    return Plan(name, session.get("saved_at"), sorted(workspace_steps.values(), key=lambda w: w.workspace),
                steps, missing, len(open_windows) - len(taken), others, pinned,
                _views(session, state, by_identity, mapping), _screens(session, mapping, by_identity),
                mapping.elsewhere, mapping.elsewhere and not follow, _modes(steps, mode_now), warm)


def _modes(steps: list[WindowStep], mode_now: Callable[[str], str] | None) -> dict[str, str]:
    """Workspace -> the Omarchy mode to put it in: the one its windows were
    saved in, where that isn't its mode now. Setups saved before modes were,
    and workspaces two saved ones now share with different modes, are left be."""
    if mode_now is None:
        return {}
    wanted: dict[str, set[str]] = {}
    for step in steps:
        if step.action in ("launch", "move", "in-place") and step.workspace and step.workspace.isdecimal():
            wanted.setdefault(step.workspace, set()).add(step.workspace_mode)
    return {ws: mode for ws, modes in sorted(wanted.items(), key=lambda kv: int(kv[0]))
            if len(modes) == 1 and (mode := next(iter(modes))) and mode != mode_now(ws)}


def app_name(app: str, entries: dict[str, DesktopEntry]) -> str:
    """An app's name from its desktop entry, for people; its class otherwise."""
    for entry in entries.values():
        if entry.wm_class == app:
            return entry.name
    entry = entries.get(f"{app}.desktop") or entries.get(f"{app.lower()}.desktop")
    return entry.name if entry else app


def _screens(session: dict, mapping: Mapping, by_identity: dict[str, Monitor]) -> list[dict]:
    """For people: where each saved screen's windows go today."""
    result = []
    for saved in session.get("monitors", []):
        identity = saved.get("identity")
        monitor = mapping.screen.get(identity)
        if monitor:
            how = "same" if identity in by_identity else ("main" if monitor is mapping.main else "side")
            result.append({"saved": identity, "was": saved.get("connector"), "to": monitor.name, "how": how})
        elif any(key[0] == identity for key in mapping.folded):
            result.append({"saved": identity, "was": saved.get("connector"), "how": "folded",
                           "to": mapping.main.name if mapping.main else None,
                           "slots": {str(s): t for (i, s), t in sorted(mapping.folded.items()) if i == identity}})
        else:
            result.append({"saved": identity, "was": saved.get("connector"), "to": None, "how": "waiting"})
    return result


def _views(session: dict, state, by_identity: dict[str, Monitor], mapping: Mapping) -> dict[str, str]:
    """Connector -> the workspace that screen showed when the session was saved."""
    views = {}
    for saved in session.get("monitors", []):
        monitor = by_identity.get(saved.get("identity")) or mapping.screen.get(saved.get("identity"))
        view = saved.get("view") or {}
        if not monitor:
            continue
        if view.get("slot"):
            number = state.workspace_for(monitor.identity, view["slot"])
            if number is not None:
                views[monitor.name] = str(number)
        elif str(view.get("workspace", "")).isdecimal():  # saved without Omarchy's blocks
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


def _join_merged(owner: str, slot: int, state, existing: dict[int, str], host: Monitor | None):
    """The workspace Omarchy merged this monitor's slot into, if it exists.

    Making the original number instead would make Omarchy renumber the merged
    one when the monitor returns. Only while it exists, though: a record
    outlives its workspace, and Omarchy forgets the record as soon as that
    number is created again, which would strand the windows."""
    for current, (original, identity) in sorted(state.merged.items()):
        if identity == owner and slot_of(original) == slot and current in existing:
            return str(current), existing.get(current) or (host.name if host else None), owner, slot, True
    return None


def _target(saved: dict, state, by_identity: dict[str, Monitor], host: Monitor | None,
            existing: dict[int, str], mapping: Mapping | None = None, follow: bool = True
            ) -> tuple[str, str | None, str | None, int | None, bool] | None:
    """(workspace, connector, owner identity, slot, waiting) for a saved window."""
    workspace = str(saved.get("workspace", ""))
    owner, slot = saved.get("monitor"), saved.get("slot")

    if not owner or not slot:
        if workspace.startswith("special:"):
            return workspace, None, None, None, False
        if workspace.isdecimal():  # saved without Omarchy's blocks
            return workspace, existing.get(int(workspace)), None, None, False
        return None

    monitor = by_identity.get(owner)
    if monitor:
        number = state.workspace_for(owner, slot)
        if number is None:  # plugged in, but Omarchy hasn't given it a block
            return None
        return str(number), monitor.name, owner, slot, False

    # Omarchy already folded this workspace: join it, so Omarchy still takes
    # it home when the monitor is back.
    if follow:
        joined = _join_merged(owner, slot, state, existing, host)
        if joined:
            return joined

    # Elsewhere: the screen it goes to by role, or a slot folded into main.
    placed = mapping.place(owner, slot) if mapping else None
    if placed:
        screen, new_slot = placed
        number = state.workspace_for(screen.identity, new_slot)
        if number is not None:
            return str(number), screen.name, screen.identity, new_slot, False

    # Not plugged in.
    joined = _join_merged(owner, slot, state, existing, host)
    if joined:
        return joined
    number = state.workspace_for(owner, slot)
    if number is None:
        return None
    return str(number), existing.get(number) or (host.name if host else None), owner, slot, True


def _match(saved: list[dict], open_windows: list[CapturedWindow], same_instance: bool,
           targets: list[str | None], tabs: dict[str, dict] | None = None
           ) -> dict[int, tuple[CapturedWindow, str]]:
    """Saved window index -> (the open window that is it, what matched).
    `targets[i]` is the workspace saved window i goes to; `tabs` the open
    browser windows' tabs by address."""
    tabs = tabs or {}

    def saved_urls(s: dict) -> list[str]:
        return (s.get("tabs") or {}).get("urls") or []

    def may_be(w: CapturedWindow, s: dict) -> bool:
        # A browser window with known tabs is only the saved one if it holds its tabs.
        have = tabs.get(w.address)
        return not saved_urls(s) or have is None or browsers.holds(have.get("urls") or [], saved_urls(s))

    pool: dict[str, list[CapturedWindow]] = {}
    for window in sorted(open_windows, key=lambda w: (w.focus < 0, w.focus)):
        pool.setdefault(window.app, []).append(window)

    tests = []
    if same_instance:
        tests.append(("window", lambda w, s: bool(w.stable_id) and w.stable_id == s.get("stable_id")))
    tests.append(("tabs", lambda w, s: bool(saved_urls(s)) and w.address in tabs))
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
                if may_be(window, s) and test(window, s):
                    result[index] = (window, how)
                    candidates.remove(window)
                    break
    return result


def _rect(client: dict) -> Rect:
    return Rect(*client["at"], *client["size"])


def _close(a: Rect, b: Rect) -> bool:
    return all(abs(p - q) <= GEOMETRY_TOLERANCE for p, q in ((a.x, b.x), (a.y, b.y), (a.w, b.w), (a.h, b.h)))
