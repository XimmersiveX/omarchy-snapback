"""Snapback command line: save, restore and switch window setups."""

from __future__ import annotations

import argparse
import functools
import json
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Callable

from . import __version__, apps, browsers, config, hypr, omarchy, plan, restore, screens, session, setup, status, watch
from .capture import Source, capture
from .geometry import monitors_from_hypr

TESTED_HYPRLAND = ("v0.56",)
# Errors Snapback explains itself; anything else is a bug and gets a traceback.
EXPECTED = (hypr.HyprError, session.SessionError, config.ConfigError, restore.RestoreError)


def cmd_inspect(args: argparse.Namespace) -> int:
    source = Source.from_dir(Path(args.source)) if args.source else Source.live()
    monitors = source.monitors()
    windows = capture(source)
    if args.json:
        print(json.dumps({
            "monitors": [{"name": m.name, "identity": m.identity, "box": m.box.__dict__,
                          "scale": m.scale, "transform": m.transform} for m in monitors],
            "windows": [w.to_dict() for w in windows],
        }, indent=2))
        return 0

    print("Monitors")
    for m in monitors:
        block = source.workspaces.blocks.get(m.identity)
        ws = f"ws {block * 10 + 1}-{block * 10 + 10}" if block is not None else "no Omarchy block"
        rotated = " portrait" if m.transform % 2 else ""
        print(f"  {m.name:<9} {m.identity}")
        print(f"            {m.box.w:g}x{m.box.h:g} at {m.box.x:g},{m.box.y:g}, "
              f"scale {m.scale:g}{rotated}, {ws}")
    print()
    print(f"Windows ({len(windows)})")
    for w in windows:
        where = f"slot {w.slot} of {w.owner}" if w.owner else w.workspace
        mode = "floating" if w.floating else "tiled"
        print(f"  {w.app}  [{mode}]  ws {w.workspace} -> {where}")
        if w.placement:
            p = w.placement
            print(f"      {p.w:g}x{p.h:g} at +{p.dx:g},+{p.dy:g} on {w.shown_on}")
    return 0


def load_resolver() -> apps.Resolver:
    """Which app made a window, with the apps installed and the config as they are now."""
    cfg = config.load()
    return apps.Resolver(apps.load_entries(apps.data_dirs()), cfg.apps, cfg.never_restore)


def save_live(source: Source, resolver: apps.Resolver,
              tabs: Callable[[list[dict]], dict] = browsers.open_tabs) -> dict:
    """The session for the live desk in `source`."""
    return session.build(source, resolver, apps.read_proc, versions_of(source),
                         marks=screens.load_marks(), tabs=tabs)


def plan_restore(saved: dict, source: Source, resolver: apps.Resolver, tabs: dict | None,
                 name: str = "") -> plan.Plan:
    """The plan for putting `saved` back. `tabs` are the open browser windows'
    tabs, or None when planning against a recording (nothing live to ask)."""
    live = {} if tabs is None else {"tabs": tabs, "cold": browsers.cold_restore}
    return plan.build(saved, source, resolver.entries, resolver.is_never, apps.default_terminal(),
                      name=name, marks=screens.load_marks(), fold=screens.load_fold(), **live)


def record(name: str, trigger: str, report: restore.Report | None = None, **details) -> None:
    """What a restore did, for the panel."""
    status.record_restore(status.restore_record(
        name, trigger, report.to_dict() if report else None, **details))


def cmd_save(args: argparse.Namespace) -> int:
    path = session.session_path(args.name)
    resolver = load_resolver()
    if args.source:  # a recording has no live processes or browsers
        source = Source.from_dir(Path(args.source))
        saved = session.build(source, resolver, lambda pid: apps.ProcInfo(), {},
                              marks=screens.load_marks())
    else:
        saved = save_live(Source.live(), resolver)

    if args.json:
        print(json.dumps(saved, indent=2))
    else:
        show_session(saved)
    if args.dry_run:
        print(f"Dry run: nothing written (would save to {path}).",
              file=sys.stderr if args.json else sys.stdout)
        return 0
    session.write(saved, path, force=args.force)
    reopen = session.restorable(saved)
    attention = sum(1 for w in saved["windows"] if w["problem"])
    if not args.json:
        print(f"Saved to {path}: {reopen} will reopen"
              + (f", {attention} need attention." if attention else "."))
    return 0


def show_session(saved: dict) -> None:
    connector = {m["identity"]: m["connector"] for m in saved["monitors"]}
    print(f"Windows ({len(saved['windows'])})")
    for w in saved["windows"]:
        where = (f"{connector.get(w['monitor'], w['monitor'])} slot {w['slot']}"
                 if w["monitor"] else w["workspace"])
        if w["launch"]:
            launch = w["launch"]
            how = launch["entry"] + (f" in {launch['cwd']}" if launch.get("cwd") else "")
            print(f"  ok    {w['app']}  [{where}]  -> {how}  ({launch['via']})")
        elif w["skip"]:
            print(f"  skip  {w['app']}  [{where}]  {w['skip']}")
        else:
            print(f"  !!    {w['app']}  [{where}]  {w['problem']}")


def cmd_restore(args: argparse.Namespace) -> int:
    if not args.dry_run and args.source:
        print("snapback: --from only works with --dry-run", file=sys.stderr)
        return 2
    path = Path(args.file) if args.file else session.session_path(args.name)
    saved = session.read(path)
    resolver = load_resolver()
    source = Source.from_dir(Path(args.source)) if args.source else Source.live()
    tabs = None if args.source else browsers.open_tabs(source.snapshot["clients"])
    result = plan_restore(saved, source, resolver, tabs, name=path.stem)
    if args.dry_run:
        if args.json:
            print(json.dumps(result.to_dict(), indent=2))
        else:
            show_plan(result, source, args.close_others)
        return 0

    with restore.Lock():
        if args.close_others:
            # Switching: keep the desk as it is now, so switching to UNDO_NAME
            # undoes this. The session being restored was read above, so switching
            # to UNDO_NAME itself swaps the two. An empty desk is kept too. Only
            # once the lock is held: a half-restored desk is no undo.
            undo = save_live(source, resolver, tabs=lambda clients: tabs)
            session.write(undo, session.session_path(session.UNDO_NAME), force=True)
        log = (lambda line: None) if args.json else (lambda line: print(f"  {line}"))
        try:
            report = restore.run(result, restore.LiveDesk(), timeout=args.timeout, log=log,
                                 close_others=args.close_others)
        except (restore.RestoreError, hypr.HyprError) as exc:
            record(path.stem, "manual", error=str(exc))
            raise
    # Check: planned again now, everything should already be in place.
    now = Source.live()
    after = plan_restore(saved, now, resolver, browsers.open_tabs(now.snapshot["clients"]))
    off = [w for w in after.windows if w.action not in ("in-place", "skip", "problem")]
    record(path.stem, "manual", report, check=[w.to_dict() for w in off])
    if args.json:
        print(json.dumps({**report.to_dict(), "check": [w.to_dict() for w in off]}, indent=2))
    else:
        show_report(report, off)
    if args.notify:
        notify(*restore_summary(report, len(off)))
    return 1 if report.count("failed") or report.errors or off else 0


def show_report(report: restore.Report, off: list[plan.WindowStep]) -> None:
    print()
    for name in report.closed:
        print(f"  ok    closed {name}")
    for name in report.kept:
        print(f"  note  {name} is still open (it may be asking to save something)")
    for name in report.pinned:
        print(f"  note  {name} left open: it, or another window of it, is pinned")
    for r in report.results:
        mark = {"placed": "ok  ", "in-place": "ok  ", "failed": "FAIL", "skipped": "skip",
                "unrestorable": "!!  "}[r.outcome]
        where = f"ws {r.step.workspace}" if r.step.workspace else ""
        print(f"  {mark}  {r.step.app}  {where}" + (f"  {r.detail}" if r.detail else ""))
    for workspace in report.rebuilt:
        print(f"  ok    layout of ws {workspace} rebuilt as saved")
    for workspace in report.reordered:
        print(f"  ok    tiles on ws {workspace} put back in their saved order")
    for extra in report.extra:
        print(f"  note  an extra window opened and was left alone: {extra}")
    for error in report.errors:
        print(f"  FAIL  {error}")
    print()
    print(f"Restored: {report.count('placed')} placed, {report.count('in-place')} already in place"
          + (f", {report.count('failed')} failed" if report.count('failed') else "")
          + (f", {report.count('skipped')} skipped" if report.count('skipped') else "")
          + (f", {report.count('unrestorable')} can't be restored" if report.count('unrestorable') else "")
          + ".")
    if off:
        print("Check: these aren't where they belong yet:")
        for w in off:
            print(f"  {w.app}  {w.action}: on ws {w.now}, should be on ws {w.workspace}")
    else:
        print("Check: every restored window is where it belongs.")


def show_plan(result: plan.Plan, source: Source, close_others: bool = False) -> None:
    connector = {m.identity: m.name for m in source.monitors()}
    print(f"Restore plan for {result.session!r}"
          + (f", saved {result.saved_at}" if result.saved_at else ""))
    for identity in result.missing_monitors:
        print(f"  note  {identity} isn't plugged in: its windows wait on another screen "
              f"and Omarchy moves them home when it's back")

    prep = [w for w in result.workspaces if w.action != "ok"]
    if prep:
        print()
        print("Workspaces first")
        for w in prep:
            whose = f" (waiting for {w.owner})" if w.waiting else ""
            if w.action == "create":
                print(f"  make  ws {w.workspace} on {w.monitor}{whose}")
            else:
                print(f"  move  ws {w.workspace} from {w.now_on} to {w.monitor}{whose}")

    print()
    print(f"Windows ({len(result.windows)})")
    for w in result.windows:
        where = f"ws {w.workspace}" if w.workspace else ""
        if w.slot:
            where += f" ({connector.get(w.owner, 'unplugged')} slot {w.slot})"
        if w.waiting and w.monitor:
            where += f", waiting on {w.monitor}"
        if w.geometry:
            g = w.geometry
            where += f", floating {g.w:g}x{g.h:g} at {g.x:g},{g.y:g}"
        if w.action == "launch":
            print(f"  open  {w.app}  -> {where}")
            print(f"            $ {' '.join(w.command)}")
        elif w.action == "move":
            by = {"title": " (same title)", "tabs": " (same tabs)",
                  "app": " (another window of this app)"}.get(w.matched_by, "")
            print(f"  move  {w.app}  ws {w.now} -> {where}{by}")
        elif w.action == "in-place":
            print(f"  ok    {w.app}  already on {where}")
        elif w.action == "skip":
            print(f"  skip  {w.app}  {w.reason}")
        else:
            print(f"  !!    {w.app}  {w.reason}")
        for note in w.notes:
            print(f"            {note}")

    if close_others and result.others:
        print()
        print(f"Close first ({len(result.others)})")
        for other in result.others:
            print(f"  close {other['name']}  on ws {other['workspace']}  ({other['title'][:60]})")
    if close_others and result.pinned:
        print()
        print(f"Left open: pinned, or the app has a pinned window ({len(result.pinned)})")
        for other in result.pinned:
            print(f"  keep  {other['name']}  on ws {other['workspace']}  ({other['title'][:60]})")
    moved = [m for m in result.screens if m["how"] != "same"]
    if moved:
        print()
        print("Screens (not where this setup was saved)")
        for m in moved:
            was = m["was"] or m["saved"]
            if m["how"] in ("main", "side"):
                print(f"  {was} -> {m['to']}" + ("  (main screen)" if m["how"] == "main" else ""))
            elif m["how"] == "folded":
                slots = ", ".join(f"{a}->{b}" for a, b in m["slots"].items())
                print(f"  {was} -> folded into {m['to']} (workspaces {slots})")
            else:
                print(f"  {was} -> waits for it to be plugged in")
    if result.views:
        print()
        print("Screens: " + ", ".join(f"{m} shows ws {ws}" for m, ws in sorted(result.views.items())))

    parts = [f"{result.count('launch')} to open", f"{result.count('move')} to move",
             f"{result.count('in-place')} already in place"]
    for action, label in (("skip", "skipped"), ("problem", "can't be restored")):
        if result.count(action):
            parts.append(f"{result.count(action)} {label}")
    print()
    left = result.untouched - (len(result.others) if close_others else 0)
    print("Dry run: " + ", ".join(parts) + "."
          + (f" {len(result.others)} to close." if close_others and result.others else "")
          + (f" {left} other open window(s) left alone." if left else ""))


def versions_of(source: Source) -> dict:
    return {"hyprland": source.snapshot["version"].get("tag"),
            "omarchy": omarchy_version(), "snapback": __version__}


@functools.cache
def omarchy_version() -> str | None:
    """Asked once per process: the watcher saves every minute."""
    return omarchy.installed_version()


def autosaver(name: str = session.DEFAULT_NAME):
    """A save function for the watcher: True when it wrote a changed session."""
    path = session.session_path(name)
    try:
        last = [watch.comparable(session.read(path))]
    except session.SessionError:
        last = [None]

    def save() -> bool:
        saved = save_live(Source.live(), load_resolver())
        if watch.comparable(saved) == last[0]:
            return False
        session.write(saved, path)
        last[0] = watch.comparable(saved)
        return True

    return save


def may_save() -> str | None:
    if watch.restore_running():
        return "a restore is running"
    return watch.shutdown_in_progress()


def cmd_watch(args: argparse.Namespace) -> int:
    log = (lambda line: print(f"snapback: {line}", flush=True)) if args.verbose else (lambda line: None)
    with watch.WatchLock():
        sock = watch.event_socket()
        watch.Watcher(autosaver(), may_save, log=log).run(watch.events_from(sock))
    return 0


def notify(title: str, body: str, urgent: bool = False) -> None:
    try:
        subprocess.run(["notify-send", "-a", "Snapback", *(["-u", "critical"] if urgent else []),
                        title, body], timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        pass


def cmd_login(args: argparse.Namespace) -> int:
    """After login: put the last session back, then keep it saved."""
    time.sleep(args.settle)  # let autostarted apps and late monitors turn up
    path = session.session_path(session.DEFAULT_NAME)
    if path.exists():
        # The session as it was when the desk went down, kept apart from the
        # autosave, so a restore that goes wrong can't lose it.
        backup = session.session_path(session.PREVIOUS_NAME)
        try:
            shutil.copy2(path, backup)
            backup.chmod(0o600)
        except OSError as exc:
            print(f"snapback: couldn't keep the session as {session.PREVIOUS_NAME!r}: {exc}",
                  file=sys.stderr, flush=True)
        name = session.DEFAULT_NAME
        try:
            saved, resolver, source = session.read(path), load_resolver(), Source.live()
            result = plan_restore(saved, source, resolver, browsers.open_tabs(source.snapshot["clients"]),
                                  name=name)
            with restore.Lock():
                report = restore.run(result, restore.LiveDesk(), timeout=args.timeout)
            print("snapback: " + ": ".join(restore_summary(report)), flush=True)  # no titles in the journal
            record(name, "login", report)
            notify(*restore_summary(report))
        except Exception as exc:  # noqa: BLE001 - whatever went wrong, autosave still starts
            if not isinstance(exc, EXPECTED):
                traceback.print_exc()
            print(f"snapback: restore failed: {exc}", file=sys.stderr, flush=True)
            record(name, "login", error=str(exc))
            notify("Snapback couldn't restore your windows",
                   f"{exc}\nYour session is kept as {session.PREVIOUS_NAME!r}.", urgent=True)
    return cmd_watch(args)


def restore_summary(report: restore.Report, misplaced: int = 0) -> tuple[str, str]:
    back = report.count("placed") + report.count("in-place")
    failed = [r.step.app for r in report.results if r.outcome in ("failed", "unrestorable")]
    title = f"Snapback restored {back} window{'s' if back != 1 else ''}"
    if failed:
        names = ", ".join(sorted(set(failed))[:3]) + ("..." if len(set(failed)) > 3 else "")
        return title, f"{len(failed)} couldn't be restored: {names}"
    if misplaced:
        return title, f"{misplaced} still out of place; restore again to finish."
    return title, "Everything is back where it was."



def cmd_delete(args: argparse.Namespace) -> int:
    path = session.session_path(args.name)
    try:
        path.unlink()
    except FileNotFoundError:
        print(f"snapback: no saved session named {args.name!r}", file=sys.stderr)
        return 1
    print(f"Deleted {path}.")
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    try:
        if args.remove:
            removed = setup.remove()
            if setup.unit_running():
                setup.stop()
            print(f"Removed {setup.hook_path()}." if removed else "Snapback wasn't set up.")
            print("Saved sessions are kept in " + str(session.sessions_dir()) + ".")
            return 0
        path = setup.install()
    except FileExistsError as exc:
        print(f"snapback: {exc}", file=sys.stderr)
        return 1
    print(f"Wrote {path}: after each login Snapback puts your windows back, then keeps them saved.")
    if args.no_start:
        return 0
    if setup.unit_running():
        print("Snapback is already watching this session.")
    else:
        setup.start("watch")
        print("Started watching this session (systemd user unit 'snapback').")
    return 0


def current_status() -> dict:
    return status.snapshot(
        hook=status.hook_state(setup.hook_path(), setup.MARKER),
        hook_path=setup.hook_path(),
        watching=setup.unit_running(),
        restoring=watch.restore_running(),
        version=__version__,
        entries=apps.load_entries(apps.data_dirs()),
        screens=screen_list(),
        fold=screens.load_fold(),
    )


def cmd_status(args: argparse.Namespace) -> int:
    snap = current_status()
    if args.json:
        print(json.dumps(snap, indent=2))
        return 0
    at_login = {"on": "on", "off": "off",
                "foreign": f"off ({setup.hook_path()} exists but wasn't written by Snapback)"}
    print(f"Restore at login: {at_login[snap['atLogin']]}")
    print(f"Autosave: {'running' if snap['watching'] else 'not running'}")
    if snap["restoring"]:
        print("A restore is running.")
    print()
    print(f"Sessions ({len(snap['sessions'])})")
    for s in snap["sessions"]:
        if "error" in s:
            print(f"  {s['name']:<10} !! {s['error']}")
            continue
        print(f"  {s['name']:<10} {s['saved_at']}  {s['windows']} window(s): {', '.join(s['apps'])}")
    last = snap["lastRestore"]
    if last:
        c = last["counts"]
        print()
        print(f"Last restore: {last['session']!r} ({last['trigger']}) at {last['at']}: "
              f"{c['placed'] + c['in-place']} back"
              + (f", {len(last['problems'])} couldn't be restored" if last["problems"] else "")
              + (f", {last['misplaced']} not where they belong" if last["misplaced"] else ""))
        for e in last["errors"]:
            print(f"  FAIL  {e}")
    return 0


def screen_list() -> list[dict]:
    """Today's screens left to right, with which one is main (marked or guessed).
    The panel asks every few seconds, so only the monitors are queried."""
    connectors = omarchy.WorkspaceState.load().connectors
    monitors = sorted(monitors_from_hypr(hypr.query("monitors"), connectors),
                      key=lambda m: (m.box.x, m.box.y))
    marks = screens.load_marks()
    main = screens.pick_main([(m.identity, m.box) for m in monitors], marks)
    names = {1: ["only"], 2: ["left", "right"], 3: ["left", "middle", "right"]}.get(len(monitors))
    return [{"connector": m.name, "identity": m.identity, "position": names[i] if names else str(i + 1),
             "main": m.identity == main, "marked": m.identity in marks} for i, m in enumerate(monitors)]


def cmd_screens(args: argparse.Namespace) -> int:
    if args.fold:
        screens.set_fold(args.fold)
    listing = screen_list()
    if args.main:
        found = [s for s in listing if args.main in (s["connector"], s["identity"])]
        if not found:
            print(f"snapback: no screen {args.main!r} is plugged in", file=sys.stderr)
            return 1
        screens.mark_main(found[0]["identity"], [s["identity"] for s in listing])
        listing = screen_list()
    if args.json:
        print(json.dumps({"screens": listing, "fold": screens.load_fold()}, indent=2))
        return 0
    for s in listing:
        main = "  main" + ("" if s["marked"] else " (guessed; mark it with --main)") if s["main"] else ""
        print(f"  {s['position']:<7} {s['connector']:<9} {s['identity']}{main}")
    fold = screens.load_fold()
    print()
    print("Folding missing screens: " + ("follow Omarchy (Omarchy still takes windows home on replug)"
                                         if fold == screens.FOLD_OMARCHY else
                                         "Snapback's order (a setup takes windows home on replug)"))
    return 0


def cmd_doctor(_: argparse.Namespace) -> int:
    problems = 0
    try:
        version = hypr.query("version")
        tag = version.get("tag", "?")
        ok = tag.startswith(TESTED_HYPRLAND)
        print(f"{'ok  ' if ok else 'warn'} Hyprland {tag}"
              + ("" if ok else f" (tested with {', '.join(TESTED_HYPRLAND)})"))
    except hypr.HyprError as exc:
        print(f"FAIL {exc}")
        return 1
    source = Source.live()
    if source.workspaces.found:
        print(f"ok   Omarchy per-monitor workspaces ({len(source.workspaces.blocks)} monitors known)")
    else:
        print("warn Omarchy per-monitor workspace file not found; workspaces will be saved by number")
    for m in source.monitors():
        if m.identity not in source.workspaces.blocks and source.workspaces.found:
            print(f"warn {m.name} ({m.identity}) has no Omarchy workspace block")
            problems += 1
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="snapback", description=__doc__.splitlines()[0])
    parser.add_argument("--version", action="version", version=f"snapback {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("inspect", help="show what a save would capture (read-only)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--from", dest="source", metavar="DIR", help="use a recorded fixture")
    p.set_defaults(func=cmd_inspect)

    p = sub.add_parser("save", help="save the open windows so they can be restored")
    p.add_argument("name", nargs="?", default=session.DEFAULT_NAME,
                   help=f"session name (default {session.DEFAULT_NAME!r})")
    p.add_argument("--dry-run", action="store_true", help="show what would be saved, write nothing")
    p.add_argument("--json", action="store_true", help="print the session as JSON")
    p.add_argument("--force", action="store_true",
                   help="replace a saved session even with one that reopens nothing")
    p.add_argument("--from", dest="source", metavar="DIR", help="use a recorded fixture")
    p.set_defaults(func=cmd_save)

    p = sub.add_parser("restore", help="put a saved session back")
    p.add_argument("name", nargs="?", default=session.DEFAULT_NAME,
                   help=f"session name (default {session.DEFAULT_NAME!r})")
    p.add_argument("--dry-run", action="store_true", help="show what would happen, change nothing")
    p.add_argument("--json", action="store_true", help="print the plan as JSON")
    p.add_argument("--file", metavar="PATH", help="read the session from this file instead")
    p.add_argument("--from", dest="source", metavar="DIR", help="plan against a recorded fixture")
    p.add_argument("--timeout", type=float, default=30.0, metavar="S",
                   help="seconds to wait for reopened apps' windows (default 30)")
    p.add_argument("--notify", action="store_true", help="say how it went in a desktop notification")
    p.add_argument("--close-others", action="store_true",
                   help="switch to the session: save the desk as 'before-switch' (switching to "
                        "that undoes it), then ask open windows the session doesn't name to close "
                        "(as their close button does; unsaved work can still ask)")
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser("delete", help="delete a saved session")
    p.add_argument("name")
    p.set_defaults(func=cmd_delete)

    p = sub.add_parser("watch", help="keep the 'last' session saved as windows change")
    p.add_argument("--verbose", action="store_true", help="say what it does")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("login", help="restore the last session, then watch (run by the login hook)")
    p.add_argument("--settle", type=float, default=3.0, metavar="S",
                   help="seconds to wait before restoring (default 3)")
    p.add_argument("--timeout", type=float, default=45.0, metavar="S",
                   help="seconds to wait for reopened apps' windows (default 45)")
    p.add_argument("--verbose", action="store_true", help="say what it does")
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("setup", help="restore at login and keep the session saved")
    p.add_argument("--remove", action="store_true", help="undo it (saved sessions are kept)")
    p.add_argument("--no-start", action="store_true", help="don't start watching this session now")
    p.set_defaults(func=cmd_setup)

    p = sub.add_parser("status", help="is Snapback on, what's saved, how the last restore went")
    p.add_argument("--json", action="store_true", help="print it as JSON (what the panel reads)")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("screens", help="list the screens; mark the main one here (setups use it "
                                       "on other screens than they were saved on)")
    p.add_argument("--main", metavar="SCREEN", help="mark this screen (connector or name) as main")
    p.add_argument("--fold", choices=(screens.FOLD_OMARCHY, screens.FOLD_SNAPBACK),
                   help="away from a setup's screens: follow Omarchy's folding (default; Omarchy "
                        "still takes windows home) or fold in Snapback's order")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_screens)

    p = sub.add_parser("doctor", help="check this system is one Snapback understands")
    p.set_defaults(func=cmd_doctor)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except EXPECTED as exc:
        print(f"snapback: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
