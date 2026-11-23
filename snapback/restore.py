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
    5. Give focus back.

A window always goes to its workspace first. Moving a window to a workspace
that doesn't exist yet makes that workspace on the window's monitor, so it is
then moved onto its owner monitor before anything else lands in it.

Snapback never closes or kills a window. Extra windows an app opens by itself
(a browser restoring its own session) are left where they are and reported.
"""

from __future__ import annotations

import fcntl
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from . import hypr
from .plan import Plan, WindowStep

FAILSAFE_MS = 120_000


class RestoreError(RuntimeError):
    pass


class Desk(Protocol):
    def clients(self) -> list[dict]: ...
    def workspaces(self) -> list[dict]: ...
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
    errors: list[str] = field(default_factory=list)  # problems outside any one window

    def count(self, outcome: str) -> int:
        return sum(1 for r in self.results if r.outcome == outcome)

    def to_dict(self) -> dict:
        return {
            "results": [{"app": r.step.app, "title": r.step.title, "action": r.step.action,
                         "outcome": r.outcome, "window": r.address, "detail": r.detail,
                         "workspace": r.step.workspace} for r in self.results],
            "extra": self.extra,
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
        log=lambda line: None) -> Report:
    report = Report()
    focused = desk.active_window()
    turned_off = desk.focus_on_activate()
    if turned_off:
        desk.set_focus_on_activate(False, FAILSAFE_MS)
    try:
        _restore(plan, desk, report, timeout, poll, log)
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
