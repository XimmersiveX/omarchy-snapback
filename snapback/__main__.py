"""Snapback command line.

    python -m snapback inspect [--json] [--from DIR]
    python -m snapback save [NAME] [--dry-run] [--json] [--force]
    python -m snapback doctor
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__, apps, config, hypr, omarchy, session
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
        print(f"Dry run: nothing written (would save to {path}).")
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

    p = sub.add_parser("doctor", help="check this system is one Snapback understands")
    p.set_defaults(func=cmd_doctor)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (hypr.HyprError, session.SessionError, config.ConfigError) as exc:
        print(f"snapback: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
