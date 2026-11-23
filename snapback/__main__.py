"""Snapback command line.

    python -m snapback inspect [--json] [--from DIR]
    python -m snapback save [NAME] [--dry-run] [--json] [--force]
    python -m snapback restore [NAME] [--dry-run] [--json] [--at-login] [--file PATH] [--from DIR] [--timeout S]
    python -m snapback doctor
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__, apps, config, hypr, omarchy, plan, restore, session
from .capture import Source, capture

TESTED_HYPRLAND = ("v0.56",)


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


def cmd_save(args: argparse.Namespace) -> int:
    path = session.session_path(args.name)
    cfg = config.load()
    resolver = apps.Resolver(apps.load_entries(apps.data_dirs()), cfg.apps, cfg.never_restore)
    if args.source:
        source = Source.from_dir(Path(args.source))
        procs = lambda pid: apps.ProcInfo()  # noqa: E731 - a recording has no live processes
        versions = {}
    else:
        source = Source.live()
        procs = apps.read_proc
        versions = {"hyprland": source.snapshot["version"].get("tag"),
                    "omarchy": omarchy.installed_version(), "snapback": __version__}
    saved = session.build(source, resolver, procs, versions)

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
    cfg = config.load()
    resolver = apps.Resolver(apps.load_entries(apps.data_dirs()), cfg.apps, cfg.never_restore)
    source = Source.from_dir(Path(args.source)) if args.source else Source.live()
    terminal = apps.default_terminal()
    result = plan.build(saved, source, resolver.entries, resolver.is_never, terminal,
                        name=path.stem, loose=args.at_login)
    if args.dry_run:
        if args.json:
            print(json.dumps(result.to_dict(), indent=2))
        else:
            show_plan(result, source)
        return 0

    with restore.Lock():
        log = (lambda line: None) if args.json else (lambda line: print(f"  {line}"))
        report = restore.run(result, restore.LiveDesk(), timeout=args.timeout, log=log)
    # Check: planned again now, everything should already be in place.
    after = plan.build(saved, Source.live(), resolver.entries, resolver.is_never, terminal,
                       loose=args.at_login)
    off = [w for w in after.windows if w.action not in ("in-place", "skip", "problem")]
    if args.json:
        print(json.dumps({**report.to_dict(), "check": [w.to_dict() for w in off]}, indent=2))
    else:
        show_report(report, off)
    return 1 if report.count("failed") or report.errors or off else 0


def show_report(report: restore.Report, off: list[plan.WindowStep]) -> None:
    print()
    for r in report.results:
        mark = {"placed": "ok  ", "in-place": "ok  ", "failed": "FAIL", "skipped": "skip",
                "unrestorable": "!!  "}[r.outcome]
        where = f"ws {r.step.workspace}" if r.step.workspace else ""
        print(f"  {mark}  {r.step.app}  {where}" + (f"  {r.detail}" if r.detail else ""))
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


def show_plan(result: plan.Plan, source: Source) -> None:
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
            by = {"title": " (same title)", "app": " (another window of this app)"}.get(w.matched_by, "")
            print(f"  move  {w.app}  ws {w.now} -> {where}{by}")
        elif w.action == "in-place":
            print(f"  ok    {w.app}  already on {where}")
        elif w.action == "skip":
            print(f"  skip  {w.app}  {w.reason}")
        else:
            print(f"  !!    {w.app}  {w.reason}")
        for note in w.notes:
            print(f"            {note}")

    parts = [f"{result.count('launch')} to open", f"{result.count('move')} to move",
             f"{result.count('in-place')} already in place"]
    for action, label in (("skip", "skipped"), ("problem", "can't be restored")):
        if result.count(action):
            parts.append(f"{result.count(action)} {label}")
    print()
    print("Dry run: " + ", ".join(parts) + "."
          + (f" {result.untouched} other open window(s) left alone." if result.untouched else ""))


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
    p.add_argument("--at-login", action="store_true",
                   help="any open window of an app may take a saved one's place (right after "
                        "login, when open windows are ones apps reopened themselves)")
    p.add_argument("--timeout", type=float, default=30.0, metavar="S",
                   help="seconds to wait for reopened apps' windows (default 30)")
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser("doctor", help="check this system is one Snapback understands")
    p.set_defaults(func=cmd_doctor)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (hypr.HyprError, session.SessionError, config.ConfigError, restore.RestoreError) as exc:
        print(f"snapback: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
