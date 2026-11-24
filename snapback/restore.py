"""Carry out a restore plan on the live desktop.

Order matters:
    0. Only when switching to a session (`close_others`): ask the open windows
       the session doesn't name to close, the way their own close button
       does, so an app with unsaved work can still ask about it. A window that
       stays open is reported, never killed. Pinned windows, and the other
       windows of an app with one pinned (a call behind its picture-in-picture),
       stay open and are reported. A browser's windows are asked only after
       step 4 has opened the browser windows with saved tabs: closing them all
       first could make the browser quit, and a browser started again brings
       back its own last session.
    1. Turn off misc:focus_on_activate, so reopened apps don't yank focus
       around. It is turned back on in a `finally`, and a timer inside
       Hyprland turns it back on anyway if Snapback dies before that.
    2. Move workspaces that sit on the wrong monitor onto their owner.
       Away from the setup's own screens, folding Snapback's way, the windows
       to be moved are parked on a hidden workspace first. Omarchy remembers which monitor each
       workspace it folded came from and sends that workspace back when the
       monitor returns; a workspace Snapback refilled would otherwise take
       the wrong windows there. Emptied, those workspaces go away, and Omarchy
       forgets them when their numbers are made again.
    3. Move open windows into place.
    4. Launch the rest, catch each new window by its app class, and place it.
       While they launch, a window rule per app opens its next window straight
       on that window's workspace, out of sight, instead of on the screen in
       front of you. Exec rules can't do that: they match the process Hyprland
       spawned, and uwsm-app hands the app to another process. A rule can't
       tell two windows of one app apart, so after each window is caught it
       points at the next one's workspace, and a window that still opens
       somewhere else is moved as before. The rules go when the launches are
       done, and a timer inside Hyprland removes them if Snapback dies first.
       Browser windows with saved tabs are opened one at a time, each caught
       before the next, as their titles can't tell them apart.
    5. Put tiled windows back in their layout. Reopened apps arrive in any
       order, and dwindle builds its tree of splits from arrival order, so a
       workspace can come back the wrong shape. Where it did, its restored
       tiled windows are parked in a hidden staging workspace and brought back
       one at a time in the saved tree's order, each time focusing the window
       to split and preselecting the side, then resized to their saved sizes
       (see layout.py). That needs the workspace on screen, so each monitor
       gets its view back afterwards. A workspace holding tiled windows the
       session doesn't know, a shape no tree of splits makes, or a layout other
       than dwindle (master, scrolling: a workspace rule can set one per
       workspace) only has its windows swapped into the tiles nearest their
       saved spots.
    6. Show on each screen the workspace it showed when the session was saved.
    7. Put windows saved full screen or full width back that way. Last, as a
       full screen window would stand in the way of laying out the tiles under
       it; a window that is full screen now but wasn't saved so leaves it in
       step 3, before it moves. Windows waiting for their monitor are left as
       they are, so they don't cover the screen hosting them.
    8. Give focus back, to a window on a workspace that is on screen. On a
       workspace with a full screen window, that window: focusing one under it
       would take its full screen away (Omarchy's on_focus_under_fullscreen).

A window always goes to its workspace first. Moving a window to a workspace
that doesn't exist yet makes that workspace on the window's monitor, so it is
then moved onto its owner monitor before anything else lands in it.

Snapback never kills a window, and closes them only in step 0. Extra windows
an app opens by itself (a browser restoring its own session) are left where
they are and reported.
"""

from __future__ import annotations

import itertools
import math
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from . import files, hypr, layout
from .capture import app_of
from .geometry import Rect, contains, logical_size
from .plan import Plan, WindowStep

FAILSAFE_MS = 120_000


class RestoreError(RuntimeError):
    pass


class Desk(Protocol):
    def clients(self) -> list[dict]: ...
    def workspaces(self) -> list[dict]: ...
    def monitors(self) -> list[dict]: ...
    def active_window(self) -> str | None: ...
    def dispatch(self, method: str, **args: object) -> None: ...
    def layout(self, message: str) -> None: ...
    def launch(self, command: list[str]) -> None: ...
    def set_open_rules(self, rules: dict[str, str], failsafe_ms: int | None = None) -> None: ...
    def focus_on_activate(self) -> bool: ...
    def set_focus_on_activate(self, on: bool, failsafe_ms: int | None = None) -> None: ...
    def sleep(self, seconds: float) -> None: ...
    def now(self) -> float: ...


class LiveDesk:
    """The real desktop, through the Hyprland adapter."""

    def clients(self) -> list[dict]:
        return hypr.query("clients")

    def workspaces(self) -> list[dict]:
        return hypr.query("workspaces")

    def monitors(self) -> list[dict]:
        return hypr.query("monitors")

    def active_window(self) -> str | None:
        return (hypr.query("activewindow") or {}).get("address")

    def dispatch(self, method: str, **args: object) -> None:
        hypr.dispatch(method, **args)

    def layout(self, message: str) -> None:
        hypr.evaluate(f"hl.dispatch(hl.dsp.layout({hypr.lua(message)}))")

    def launch(self, command: list[str]) -> None:
        # Its own session, so the app outlives Snapback; uwsm-app moves it into
        # an app scope of its own anyway.
        subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)

    def set_open_rules(self, rules: dict[str, str], failsafe_ms: int | None = None) -> None:
        """Make these Snapback's window rules (app class -> workspace a new
        window of it opens on, silently), replacing the ones before."""
        off = ("for _, name in ipairs(_G.snapback_open or {}) do "
               "hl.window_rule({ name = name, enabled = false }) end _G.snapback_open = {} ")
        code = off
        for i, (app, workspace) in enumerate(sorted(rules.items())):
            name = f"snapback-open-{i}"
            code += (f"hl.window_rule({{ name = {hypr.lua(name)}, enabled = true, "
                     f"match = {{ class = {hypr.lua('^' + _regex(app) + '$')} }}, "
                     f"workspace = {hypr.lua(workspace + ' silent')} }}) "
                     f"table.insert(_G.snapback_open, {hypr.lua(name)}) ")
        token = f"{os.getpid()}-{time.monotonic_ns()}"
        code += f"_G.snapback_open_token = {hypr.lua(token)} "
        if rules and failsafe_ms:
            code += (f"hl.timer(function() if _G.snapback_open_token == {hypr.lua(token)} then {off}end end, "
                     f"{{type=\"oneshot\", timeout={failsafe_ms}}})")
        hypr.evaluate(code)

    def focus_on_activate(self) -> bool:
        return bool(hypr.option("misc:focus_on_activate"))

    def set_focus_on_activate(self, on: bool, failsafe_ms: int | None = None) -> None:
        setting = f"hl.config({{misc={{focus_on_activate={hypr.lua(on)}}}}})"
        if failsafe_ms:
            # The timer only undoes the change while this restore still owns it.
            token = f"{os.getpid()}-{time.monotonic_ns()}"
            hypr.evaluate(
                f"{setting} _G.snapback_restore = {hypr.lua(token)} "
                f"hl.timer(function() if _G.snapback_restore == {hypr.lua(token)} then "
                f"hl.config({{misc={{focus_on_activate=true}}}}) _G.snapback_restore = nil end end, "
                f"{{type=\"oneshot\", timeout={failsafe_ms}}})")
        else:
            hypr.evaluate(f"{setting} _G.snapback_restore = nil")

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def now(self) -> float:
        return time.monotonic()


@dataclass
class Result:
    step: WindowStep
    outcome: str                  # placed, in-place, failed, skipped, unrestorable
    address: str | None = None
    detail: str | None = None


@dataclass
class Report:
    results: list[Result] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)   # "class (address)" of unasked-for windows
    reordered: list[str] = field(default_factory=list)  # workspaces whose tiles were swapped
    rebuilt: list[str] = field(default_factory=list)    # workspaces whose layout was rebuilt
    other_layout: dict[str, str] = field(default_factory=dict)  # ws -> non-dwindle layout: swapped, not rebuilt
    errors: list[str] = field(default_factory=list)  # problems outside any one window
    closed: list[str] = field(default_factory=list)  # names of windows closed (switching)
    kept: list[str] = field(default_factory=list)    # asked to close, still open
    pinned: list[str] = field(default_factory=list)  # not closed: pinned, or that app has a pinned window

    def count(self, outcome: str) -> int:
        return sum(1 for r in self.results if r.outcome == outcome)

    def to_dict(self) -> dict:
        return {
            "results": [{"app": r.step.app, "title": r.step.title, "action": r.step.action,
                         "outcome": r.outcome, "window": r.address, "detail": r.detail,
                         "workspace": r.step.workspace} for r in self.results],
            "extra": self.extra,
            "reordered": self.reordered,
            "rebuilt": self.rebuilt,
            "otherLayout": self.other_layout,
            "errors": self.errors,
            "closed": self.closed,
            "kept": self.kept,
            "pinned": self.pinned,
        }


def lock_path() -> Path:
    return files.runtime_dir() / "snapback-restore.lock"


class Lock(files.Lock):
    """Only one restore at a time: two would launch every app twice."""

    def __init__(self, path: Path | None = None):
        super().__init__(path or lock_path(), RestoreError("another restore is already running"))


def run(plan: Plan, desk: Desk, timeout: float = 30.0, poll: float = 0.25,
        log=lambda line: None, settle: float = 0.3, close_others: bool = False,
        close_wait: float = 5.0) -> Report:
    report = Report()
    if close_others:
        report.pinned = [p["name"] for p in plan.pinned]
    tabbed = {s.app for s in plan.windows if s.action == "launch" and s.tabs}
    later = [o for o in plan.others if o["app"] in tabbed] if close_others else []
    if close_others:
        _close_others([o for o in plan.others if o["app"] not in tabbed], desk, report, close_wait, poll, log)
    focused = desk.active_window()
    turned_off = desk.focus_on_activate()
    if turned_off:
        desk.set_focus_on_activate(False, FAILSAFE_MS)
    try:
        _restore(plan, desk, report, timeout, poll, log)
        if later:
            _close_others(later, desk, report, close_wait, poll, log)
        _arrange_tiles(desk, report, settle, log)
        if plan.views:
            _show_views(desk, plan.views, report)
        _fullscreen(desk, report, log)
    finally:
        if turned_off:
            try:
                desk.set_focus_on_activate(True)
            except hypr.HyprError as exc:
                report.errors.append(f"couldn't turn focus_on_activate back on: {exc}")
        _refocus(desk, focused, plan, report)
    return report


def _close_others(others: list[dict], desk: Desk, report: Report, wait: float, poll: float, log) -> None:
    """Ask each of these windows the session doesn't name to close, and wait
    a moment for them to go."""
    if not others:
        return
    asked = {}
    for other in others:
        try:
            desk.dispatch("window.close", window=f"address:{other['address']}")
            asked[other["address"]] = other["name"]
            log(f"asked {other['name']} to close")
        except hypr.HyprError as exc:
            report.errors.append(f"couldn't close {other['name']}: {exc}")
    deadline = desk.now() + wait
    while True:
        open_now = {c["address"] for c in desk.clients()}
        left = [a for a in asked if a in open_now]
        if not left or desk.now() >= deadline:
            break
        desk.sleep(poll)
    report.closed += [name for a, name in asked.items() if a not in left]
    report.kept += [asked[a] for a in left]


def _restore(plan: Plan, desk: Desk, report: Report, timeout: float, poll: float, log) -> None:
    for ws in plan.workspaces:
        if ws.action == "move":
            try:
                desk.dispatch("workspace.move", workspace=str(ws.workspace), monitor=ws.monitor)
                log(f"moved ws {ws.workspace} to {ws.monitor}")
            except hypr.HyprError as exc:
                report.errors.append(f"couldn't move ws {ws.workspace} to {ws.monitor}: {exc}")

    if plan.park:
        _park([s.window for s in plan.windows if s.action == "move" and s.window], desk, report)

    launches = []
    for step in plan.windows:
        if step.action == "move":
            report.results.append(_place(desk, step, step.window, log))
        elif step.action == "in-place":
            report.results.append(Result(step, "in-place", step.window))
        elif step.action == "launch":
            launches.append(step)
        elif step.action == "problem":
            report.results.append(Result(step, "unrestorable", detail=step.reason))
        else:
            report.results.append(Result(step, "skipped", detail=step.reason))
    if launches:
        _launch([s for s in launches if not s.tabs], desk, report, timeout, poll, log)
        for step in launches:
            if step.tabs:
                _launch([step], desk, report, timeout, poll, log)


def _launch(launches: list[WindowStep], desk: Desk, report: Report, timeout: float, poll: float, log) -> None:
    """Launch these, catch each new window by its app class, and place it."""
    if not launches:
        return
    rules: dict[str, str] = {}

    def aim(pending: list[WindowStep]) -> None:
        # Each app's next window opens on the workspace of its first window still to come.
        nonlocal rules
        wanted = {}
        for step in pending:
            if step.workspace and step.app not in wanted:
                wanted[step.app] = step.workspace
        if wanted != rules:
            try:
                desk.set_open_rules(wanted, int(timeout * 1000) + 10_000)
                rules = wanted
            except hypr.HyprError as exc:
                log(f"couldn't set the window rules that open windows in place: {exc}")

    aim(launches)
    try:
        _launch_and_catch(launches, desk, report, timeout, poll, log, aim)
    finally:
        if rules:
            try:
                desk.set_open_rules({})
            except hypr.HyprError as exc:
                report.errors.append(f"couldn't remove Snapback's window rules: {exc}")


def _launch_and_catch(launches: list[WindowStep], desk: Desk, report: Report, timeout: float, poll: float,
                      log, aim) -> None:
    before = {c["address"] for c in desk.clients()}
    for step in launches:
        try:
            desk.launch(step.command)
            log(f"launched {' '.join(step.command)}")
        except OSError as exc:
            report.results.append(Result(step, "failed", detail=f"couldn't run {step.command[0]}: {exc}"))
    pending = [s for s in launches if not any(r.step is s for r in report.results)]
    wanted = {s.app for s in pending}
    claimed: set[str] = set()

    deadline = desk.now() + timeout
    while pending and desk.now() < deadline:
        fresh = [c for c in desk.clients()
                 if c["address"] not in before and c["address"] not in claimed and c.get("mapped", True)]
        for exact in (True, False):
            for step in list(pending):
                for client in fresh:
                    if client["address"] in claimed or app_of(client) != step.app:
                        continue
                    if exact and client.get("title") != step.title:
                        continue
                    claimed.add(client["address"])
                    pending.remove(step)
                    aim(pending)
                    report.results.append(_place(desk, step, client["address"], log))
                    break
        if pending:
            desk.sleep(poll)

    for step in pending:
        report.results.append(Result(step, "failed", detail=f"no {step.app} window appeared within {timeout:g}s"))
    for client in desk.clients():
        if client["address"] not in before and client["address"] not in claimed and app_of(client) in wanted:
            report.extra.append(f"{app_of(client)} ({client['address']})")


STAGING = "special:snapback-staging"
MODES = {1: "maximized", 2: "fullscreen"}  # Hyprland's names for fullscreen states 1 and 2


def _unset_fullscreen(desk: Desk, window: str, now: int) -> None:
    # Only works with the name of the mode the window is in (checked live).
    desk.dispatch("window.fullscreen", mode=MODES.get(now, "fullscreen"), action="unset", window=window)


def _fullscreen(desk: Desk, report: Report, log) -> None:
    """Each restored window back to the way it filled its screen."""
    clients = {c["address"]: c for c in desk.clients()}
    for r in report.results:
        client = clients.get(r.address) if r.outcome in ("placed", "in-place") else None
        if client is None or r.step.waiting:
            continue
        want = r.step.fullscreen
        if not want or int(client.get("fullscreen") or 0) == want:
            continue  # leaving full screen happened before the window moved
        try:
            # Setting a mode switches straight from the other one (checked live).
            desk.dispatch("window.fullscreen", mode=MODES[want], action="set", window=f"address:{r.address}")
            log(f"{MODES[want]}: {r.step.app}")
        except hypr.HyprError as exc:
            report.errors.append(f"couldn't put {r.step.app} back {MODES[want]}: {exc}")


def _park(addresses: list[str], desk: Desk, report: Report) -> None:
    for address in addresses:
        try:
            desk.dispatch("window.move", window=f"address:{address}", workspace=STAGING, follow=False)
        except hypr.HyprError as exc:
            report.errors.append(f"couldn't park a window before moving it: {exc}")


def _arrange_tiles(desk: Desk, report: Report, settle: float, log) -> None:
    """Give each workspace's restored tiled windows their saved layout back."""
    restored = [r for r in report.results
                if r.outcome in ("placed", "in-place") and r.address and r.step.tile_box]
    groups: dict[str, list[Result]] = {}
    for r in restored:
        groups.setdefault(r.step.workspace or "", []).append(r)
    groups = {ws: rs for ws, rs in groups.items() if len(rs) > 1}
    if not groups:
        return
    desk.sleep(settle)  # let the layout settle before reading tile positions

    monitors = desk.monitors()
    boxes = {}
    for m in monitors:
        w, h = logical_size(m["width"], m["height"], m["scale"], m.get("transform", 0))
        boxes[m["id"]] = Rect(m["x"], m["y"], w, h)
    by_name = {m["name"]: m["id"] for m in monitors}
    views = {m["name"]: m["activeWorkspace"]["name"] for m in monitors}
    clients = {c["address"]: c for c in desk.clients()}
    # Each workspace's own layout: a workspace rule can give one a layout
    # other than general:layout. Only dwindle keeps every tile inside its
    # monitor, so only there is a tile outside it measured on another one.
    layouts = {w["name"]: w.get("tiledLayout") for w in desk.workspaces()}

    for workspace, results in groups.items():
        now = {}
        for r in results:
            c = clients.get(r.address)
            box = boxes.get(c["monitor"]) if c else None
            if not c or not box or c.get("floating") or c["workspace"]["name"] != workspace:
                break
            now[r.address] = _tile_box(c, box, boxes.values() if layouts.get(workspace) == "dwindle" else [])
        else:
            saved = {r.address: r.step.tile_box for r in results}
            # The right tiles, perhaps holding the wrong windows: swapping is
            # enough (and none are needed when each is in its own tile).
            boxes_now = [now[r.address] for r in results]
            best = _best([r.step.tile for r in results], [_center(b) for b in boxes_now])
            if layout.same_shape({i: r.step.tile_box for i, r in enumerate(results)},
                                 {i: boxes_now[best[i]] for i in range(len(results))}):
                _swap_into_place(desk, report, workspace, results, best, log)
                continue
            tree = layout.split_tree(saved)
            monitor = results[0].step.monitor
            foreign = any(not c.get("floating") and c["workspace"]["name"] == workspace and a not in saved
                          for a, c in clients.items())
            tiled = layouts.get(workspace)
            if tiled != "dwindle":
                # Rebuilding drives dwindle's splits; master, scrolling and
                # the rest get their windows swapped into the nearest tiles.
                report.other_layout[workspace] = tiled or "unknown"
                log(f"ws {workspace} uses the {tiled or 'unknown'} layout; swapping tiles, not rebuilding")
            elif (tree is not None and not foreign and monitor in by_name and workspace.isdecimal()
                    and not any(r.step.waiting for r in results)):
                try:
                    box = boxes[by_name[monitor]]
                    sizes = {a: (round((b[2] - b[0]) * box.w), round((b[3] - b[1]) * box.h))
                             for a, b in saved.items()}
                    _rebuild(desk, workspace, monitor, tree, sizes, settle)
                    report.rebuilt.append(workspace)
                    log(f"rebuilt the layout of ws {workspace}")
                except hypr.HyprError as exc:
                    report.errors.append(f"couldn't rebuild the layout of ws {workspace}: {exc}")
                continue
            _swap_into_place(desk, report, workspace, results, best, log)

    if report.rebuilt:
        _show_views(desk, views, report)


def _rebuild(desk: Desk, workspace: str, monitor: str, tree: layout.Node,
             sizes: dict[str, tuple[int, int]], settle: float) -> None:
    """Rebuild a workspace's split tree from its windows, as layout.py explains."""
    start, steps = layout.build_steps(tree)
    windows = layout.leaves(tree)
    sel = lambda address: f"address:{address}"  # noqa: E731
    for address in windows:
        desk.dispatch("window.move", window=sel(address), workspace=STAGING, follow=False)
    try:
        desk.dispatch("focus", monitor=monitor)
        desk.dispatch("focus", workspace=workspace)
        desk.dispatch("window.move", window=sel(start), workspace=workspace, follow=False)
        for target, side, added in steps:
            desk.dispatch("focus", window=sel(target))
            desk.layout(f"preselect {side}")
            desk.dispatch("window.move", window=sel(added), workspace=workspace, follow=False)
        desk.sleep(settle)
        for _ in range(2):  # a second pass settles splits the first pass moved
            for address in windows:
                w, h = sizes[address]
                desk.dispatch("window.resize", window=sel(address), x=w, y=h, exact=True)
    finally:
        # Never leave a window parked, whatever went wrong.
        for c in desk.clients():
            if c["address"] in windows and c["workspace"]["name"] == STAGING:
                try:
                    desk.dispatch("window.move", window=sel(c["address"]), workspace=workspace, follow=False)
                except hypr.HyprError:
                    pass


def _show_views(desk: Desk, views: dict[str, str], report: Report) -> None:
    """Put each monitor back on the workspace it was showing."""
    current = {m["name"]: m["activeWorkspace"]["name"] for m in desk.monitors()}
    for monitor, workspace in views.items():
        if current.get(monitor) != workspace:
            try:
                desk.dispatch("focus", monitor=monitor)
                desk.dispatch("focus", workspace=workspace)
            except hypr.HyprError as exc:
                report.errors.append(f"couldn't put {monitor} back on ws {workspace}: {exc}")


def _swap_into_place(desk: Desk, report: Report, workspace: str, results: list[Result],
                     best: list[int], log) -> None:
    """Swap windows so window i ends up in tile best[i] (see _best)."""
    swaps = _swaps_for(best)
    if not swaps:
        return
    try:
        for i, j in swaps:
            desk.dispatch("window.swap", window=f"address:{results[i].address}",
                          target=f"address:{results[j].address}")
        report.reordered.append(workspace)
        log(f"put the tiles of ws {workspace} back in order")
    except hypr.HyprError as exc:
        report.errors.append(f"couldn't reorder ws {workspace}: {exc}")


def _tile_box(client: dict, box: Rect, boxes) -> tuple[float, float, float, float]:
    """A tiled window's box as fractions of the monitor it was laid out on.

    A hidden workspace moved from another monitor keeps tile positions laid
    out there until it is shown, so those are measured on that monitor, one
    of `boxes` (empty where tiles can lie outside their monitor, as
    scrolling's columns do).
    """
    x, y = client["at"]
    w, h = client["size"]
    if not contains(box, x + w / 2, y + h / 2):
        box = next((b for b in boxes if contains(b, x + w / 2, y + h / 2)), box)
    return ((x - box.x) / box.w, (y - box.y) / box.h, (x + w - box.x) / box.w, (y + h - box.y) / box.h)


def _center(box: tuple[float, float, float, float]) -> tuple[float, float]:
    return (box[0] + box[2]) / 2, (box[1] + box[3]) / 2


def _best(saved: list[tuple[float, float]], now: list[tuple[float, float]]) -> list[int]:
    """For each window i, the tile best[i] nearest its saved spot, with no two
    windows in one tile. `now[k]` is the tile window k is in."""
    n = len(saved)
    cost = lambda i, t: math.dist(saved[i], now[t])  # noqa: E731
    if n <= 7:
        best = list(min(itertools.permutations(range(n)), key=lambda p: sum(cost(i, p[i]) for i in range(n))))
    else:  # greedy beyond 7! permutations
        best, free = [0] * n, set(range(n))
        for i in sorted(range(n), key=lambda i: min(cost(i, t) for t in range(n))):
            best[i] = min(free, key=lambda t: cost(i, t))
            free.remove(best[i])
    if sum(cost(i, i) for i in range(n)) - sum(cost(i, best[i]) for i in range(n)) < 1e-6:
        return list(range(n))  # no better than now: leave them
    return best


def _swaps_for(best: list[int]) -> list[tuple[int, int]]:
    """Swaps that put window i into tile best[i]."""
    n = len(best)

    holder = list(range(n))   # holder[t] = window in tile t
    where = list(range(n))    # where[i] = tile window i is in
    swaps = []
    for i in range(n):
        target = best[i]
        if where[i] == target:
            continue
        other = holder[target]
        swaps.append((i, other))
        holder[where[i]], holder[target] = other, i
        where[other], where[i] = where[i], target
    return swaps


def _place(desk: Desk, step: WindowStep, address: str, log) -> Result:
    """Put one window on its workspace, then float it and size it as saved."""
    window = f"address:{address}"
    try:
        client = _client(desk, address)
        if client is None:
            return Result(step, "failed", address, "the window closed")
        now = int(client.get("fullscreen") or 0)
        if now and now != step.fullscreen and not step.waiting:
            _unset_fullscreen(desk, window, now)
        if step.workspace and client["workspace"]["name"] != step.workspace:
            desk.dispatch("window.move", window=window, workspace=step.workspace, follow=False)
        if step.monitor and step.workspace and step.workspace.isdecimal():
            number = int(step.workspace)
            on = next((w["monitor"] for w in desk.workspaces() if w["id"] == number), None)
            if on and on != step.monitor:
                desk.dispatch("workspace.move", workspace=step.workspace, monitor=step.monitor)
        if bool(client.get("floating")) != step.floating:
            desk.dispatch("window.float", window=window, action="on" if step.floating else "off")
        if step.geometry:
            g = step.geometry
            desk.dispatch("window.resize", window=window, x=g.w, y=g.h, exact=True)
            desk.dispatch("window.move", window=window, x=g.x, y=g.y, exact=True)
    except hypr.HyprError as exc:
        return Result(step, "failed", address, str(exc))
    log(f"placed {step.app} on ws {step.workspace}")
    return Result(step, "placed", address)


def _refocus(desk: Desk, focused: str | None, plan: Plan, report: Report) -> None:
    """Back to the window that had focus, or the session's last-used one, but
    only to a window on screen: focusing one on a hidden workspace would
    switch its screen away from the view just restored."""
    shown = set()
    for m in desk.monitors():
        shown.add(m["activeWorkspace"]["name"])
        special = (m.get("specialWorkspace") or {}).get("name")
        if special:
            shown.add(special)
    clients = [c for c in desk.clients() if c["workspace"]["name"] in shown]
    present = {c["address"] for c in clients}
    target = focused if focused in present else None
    if target is None:
        # Windows are saved most recently focused first.
        order = {id(step): i for i, step in enumerate(plan.windows)}
        for result in sorted(report.results, key=lambda r: order.get(id(r.step), len(order))):
            if result.address in present and result.outcome in ("placed", "in-place"):
                target = result.address
                break
    if target:
        workspace = next(c["workspace"]["name"] for c in clients if c["address"] == target)
        target = next((c["address"] for c in clients if c["workspace"]["name"] == workspace
                       and c.get("fullscreen")), target)
        try:
            desk.dispatch("focus", window=f"address:{target}")
        except hypr.HyprError as exc:
            report.errors.append(f"couldn't give focus back: {exc}")


def _regex(text: str) -> str:
    """`text` as a regular expression that matches just it."""
    return "".join("\\" + ch if ch in ".^$*+?()[]{}|\\" else ch for ch in text)


def _client(desk: Desk, address: str) -> dict | None:
    return next((c for c in desk.clients() if c["address"] == address), None)


