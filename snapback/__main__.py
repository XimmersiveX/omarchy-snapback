"""Snapback command line.

    python -m snapback inspect [--json] [--from DIR]
    python -m snapback doctor
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__, hypr
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

    p = sub.add_parser("doctor", help="check this system is one Snapback understands")
    p.set_defaults(func=cmd_doctor)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except hypr.HyprError as exc:
        print(f"snapback: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
