"""Carry out a restore plan on the live desktop.

Order matters:
    1. Turn off misc:focus_on_activate, so reopened apps don't yank focus
       around. It is turned back on in a `finally`, and a timer inside
       Hyprland turns it back on anyway if Snapback dies before that.
    2. Move workspaces that sit on the wrong monitor onto their owner.
    3. Move open windows into place.
    4. Launch the rest, catch each new window by its app class, and place it.
       A launched window first appears wherever Hyprland opens it and is then
       moved silently. Exec rules can't do better: they match the process
       Hyprland spawned, and uwsm-app hands the app to another process.
    5. Put tiled windows back in their order: within each workspace, every
       restored tiled window gets the tile nearest to where it was (as a
       fraction of the screen), by swapping windows between tiles. That works
       whatever shape the layout made, and only ever swaps restored windows.
    6. Give focus back.

A window always goes to its workspace first. Moving a window to a workspace
that doesn't exist yet makes that workspace on the window's monitor, so it is
then moved onto its owner monitor before anything else lands in it.

Snapback never closes or kills a window. Extra windows an app opens by itself
(a browser restoring its own session) are left where they are and reported.
"""

from __future__ import annotations

import fcntl
import itertools
import math
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from . import hypr
from .geometry import logical_size
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
    def launch(self, command: list[str]) -> None: ...
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

    def launch(self, command: list[str]) -> None:
        # Its own session, so the app outlives Snapback; uwsm-app moves it into
        # an app scope of its own anyway.
        subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)

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
    outcome: str                  # placed, in-place, failed, skipped
    address: str | None = None
    detail: str | None = None


@dataclass
class Report:
    results: list[Result] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)   # "class (address)" of unasked-for windows
    reordered: list[str] = field(default_factory=list)  # workspaces whose tiles were swapped
    errors: list[str] = field(default_factory=list)  # problems outside any one window

    def count(self, outcome: str) -> int:
        return sum(1 for r in self.results if r.outcome == outcome)

    def to_dict(self) -> dict:
        return {
            "results": [{"app": r.step.app, "title": r.step.title, "action": r.step.action,
                         "outcome": r.outcome, "window": r.address, "detail": r.detail,
                         "workspace": r.step.workspace} for r in self.results],
            "extra": self.extra,
            "reordered": self.reordered,
            "errors": self.errors,
        }


def lock_path() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    return Path(base) / "snapback-restore.lock"


class Lock:
    """Only one restore at a time: two would launch every app twice."""

    def __init__(self, path: Path | None = None):
        self.path = path or lock_path()
        self.file = None

    def __enter__(self) -> "Lock":
        self.file = open(self.path, "w")
        try:
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.file.close()
            raise RestoreError("another restore is already running") from exc
        return self

    def __exit__(self, *exc) -> None:
        if self.file:
            fcntl.flock(self.file, fcntl.LOCK_UN)
            self.file.close()


def run(plan: Plan, desk: Desk, timeout: float = 30.0, poll: float = 0.25,
        log=lambda line: None, settle: float = 0.3) -> Report:
    report = Report()
    focused = desk.active_window()
    turned_off = desk.focus_on_activate()
    if turned_off:
        desk.set_focus_on_activate(False, FAILSAFE_MS)
    try:
        _restore(plan, desk, report, timeout, poll, log)
        _order_tiles(desk, report, settle, log)
    finally:
        if turned_off:
            try:
                desk.set_focus_on_activate(True)
            except hypr.HyprError as exc:
                report.errors.append(f"couldn't turn focus_on_activate back on: {exc}")
        _refocus(desk, focused, plan, report)
    return report


def _restore(plan: Plan, desk: Desk, report: Report, timeout: float, poll: float, log) -> None:
    for ws in plan.workspaces:
        if ws.action == "move":
            try:
                desk.dispatch("workspace.move", workspace=str(ws.workspace), monitor=ws.monitor)
                log(f"moved ws {ws.workspace} to {ws.monitor}")
            except hypr.HyprError as exc:
                report.errors.append(f"couldn't move ws {ws.workspace} to {ws.monitor}: {exc}")

    launches = []
    for step in plan.windows:
        if step.action == "move":
            report.results.append(_place(desk, step, step.window, log))
        elif step.action == "in-place":
            report.results.append(Result(step, "in-place", step.window))
        elif step.action == "launch":
            launches.append(step)
        else:
            report.results.append(Result(step, "skipped", detail=step.reason))
    if not launches:
        return

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
                    if client["address"] in claimed or _class(client) != step.app:
                        continue
                    if exact and client.get("title") != step.title:
                        continue
                    claimed.add(client["address"])
                    pending.remove(step)
                    report.results.append(_place(desk, step, client["address"], log))
                    break
        if pending:
            desk.sleep(poll)

    for step in pending:
        report.results.append(Result(step, "failed", detail=f"no {step.app} window appeared within {timeout:g}s"))
    for client in desk.clients():
        if client["address"] not in before and client["address"] not in claimed and _class(client) in wanted:
            report.extra.append(f"{_class(client)} ({client['address']})")


def _order_tiles(desk: Desk, report: Report, settle: float, log) -> None:
    """Swap restored tiled windows so each sits in the tile nearest its saved spot."""
    restored = [r for r in report.results
                if r.outcome in ("placed", "in-place") and r.address and r.step.tile]
    groups: dict[str, list[Result]] = {}
    for r in restored:
        groups.setdefault(r.step.workspace or "", []).append(r)
    groups = {ws: rs for ws, rs in groups.items() if len(rs) > 1}
    if not groups:
        return
    desk.sleep(settle)  # let the layout settle before reading tile positions

    boxes = {}
    for m in desk.monitors():
        w, h = logical_size(m["width"], m["height"], m["scale"], m.get("transform", 0))
        boxes[m["id"]] = (m["x"], m["y"], w, h)
    clients = {c["address"]: c for c in desk.clients()}

    for workspace, results in groups.items():
        tiles = {}
        for r in results:
            c = clients.get(r.address)
            box = boxes.get(c["monitor"]) if c else None
            if not c or not box or c.get("floating") or c["workspace"]["name"] != workspace:
                break
            x, y, w, h = box
            tiles[r.address] = ((c["at"][0] - x + c["size"][0] / 2) / w,
                                (c["at"][1] - y + c["size"][1] / 2) / h)
        else:
            swaps = _swaps([r.step.tile for r in results], [tiles[r.address] for r in results])
            if not swaps:
                continue
            try:
                for i, j in swaps:
                    desk.dispatch("window.swap", window=f"address:{results[i].address}",
                                  target=f"address:{results[j].address}")
                report.reordered.append(workspace)
                log(f"put the tiles of ws {workspace} back in order")
            except hypr.HyprError as exc:
                report.errors.append(f"couldn't reorder ws {workspace}: {exc}")


def _swaps(saved: list[tuple[float, float]], now: list[tuple[float, float]]) -> list[tuple[int, int]]:
    """Swaps (window i, window j) that move each window to the tile nearest its
    saved spot. `now[k]` is the tile window k is in."""
    n = len(saved)
    cost = lambda i, t: math.dist(saved[i], now[t])  # noqa: E731
    if n <= 7:
        best = min(itertools.permutations(range(n)), key=lambda p: sum(cost(i, p[i]) for i in range(n)))
    else:  # greedy beyond 7! permutations
        best, free = [0] * n, set(range(n))
        for i in sorted(range(n), key=lambda i: min(cost(i, t) for t in range(n))):
            best[i] = min(free, key=lambda t: cost(i, t))
            free.remove(best[i])
    current_total = sum(cost(i, i) for i in range(n))
    if current_total - sum(cost(i, best[i]) for i in range(n)) < 1e-6:
        return []

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
        if step.workspace and client["workspace"]["name"] != step.workspace:
            desk.dispatch("window.move", window=window, workspace=step.workspace, follow=False)
        if step.monitor and step.workspace and step.workspace.isdigit():
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
    """Back to the window that had focus, or the session's last-used one."""
    present = {c["address"] for c in desk.clients()}
    target = focused if focused in present else None
    if target is None:
        # Windows are saved most recently focused first.
        order = {id(step): i for i, step in enumerate(plan.windows)}
        for result in sorted(report.results, key=lambda r: order.get(id(r.step), len(order))):
            if result.address in present and result.outcome in ("placed", "in-place"):
                target = result.address
                break
    if target:
        try:
            desk.dispatch("focus", window=f"address:{target}")
        except hypr.HyprError as exc:
            report.errors.append(f"couldn't give focus back: {exc}")


def _client(desk: Desk, address: str) -> dict | None:
    return next((c for c in desk.clients() if c["address"] == address), None)


def _class(client: dict) -> str:
    return client.get("initialClass") or client.get("class") or ""
