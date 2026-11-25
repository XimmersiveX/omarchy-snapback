"""Record this machine's desktop as test data, and scrub it before it's committed.

    python3 tools/fixture.py record NAME   # raw, private: notes/fixtures-raw/NAME/ (never committed)
    python3 tools/fixture.py scrub NAME    # safe copy:    tests/fixtures/NAME/

Scrubbing replaces window titles, the home folder and username, and monitor
serial numbers with consistent placeholders, then refuses to write anything
if a private value is still present.
"""

from __future__ import annotations

import argparse
import getpass
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from snapback import hypr  # noqa: E402
from snapback.omarchy import BLOCKS_FILE, MERGED_FILE, state_files  # noqa: E402

RAW = ROOT / "notes" / "fixtures-raw"  # notes/ is git-ignored
SAFE = ROOT / "tests" / "fixtures"
TITLE_KEYS = {"title", "initialTitle", "lastwindowtitle", "xdgTag", "xdgDescription"}
STATE_FILES = (BLOCKS_FILE, MERGED_FILE)


def record_files(directory: Path) -> None:
    """Write a live snapshot and Omarchy's state files into `directory`."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "snapshot.json").write_text(json.dumps(hypr.snapshot(), indent=2) + "\n")
    # Fixtures keep Omarchy 4.5's file names whatever layout the live files use.
    for name, path in zip(STATE_FILES, state_files()):
        if path.exists():
            (directory / name).write_text(path.read_text())


def record(name: str, note: str) -> None:
    target = RAW / name
    record_files(target)
    meta = {"note": note, "omarchy": _pacman_version("omarchy")}
    (target / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    print(f"recorded {target.relative_to(ROOT)} (private, not committed)")


def scrub(name: str) -> None:
    source, target = RAW / name, SAFE / name
    snapshot = json.loads((source / "snapshot.json").read_text())

    home = str(Path.home())
    user = getpass.getuser()
    replacements = {home: "/home/user", home.replace("/", "_"): "_home_user", **serial_map()}
    titles: dict[str, str] = {}

    def text(value: str) -> str:
        for old, new in replacements.items():
            value = value.replace(old, new)
        return value

    def walk(node, key=None):
        if isinstance(node, dict):
            return {k: walk(v, k) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v, key) for v in node]
        if isinstance(node, str):
            if key in TITLE_KEYS and node:
                return titles.setdefault(node, f"Window {len(titles) + 1}")
            return text(node)
        return node

    clean = walk(snapshot)
    files = {"snapshot.json": json.dumps(clean, indent=2) + "\n"}
    for state in STATE_FILES:
        if (source / state).exists():
            files[state] = text((source / state).read_text())
    if (source / "meta.json").exists():
        files["meta.json"] = text((source / "meta.json").read_text())

    secrets = [home, home.replace("/", "_"), *replacements.keys(), *titles.keys()]
    leaks = [s for s in secrets if s and any(s in body for body in files.values())]
    if any(re.search(rf"\b{re.escape(user)}\b", body, re.IGNORECASE) for body in files.values()):
        leaks.append(f"username {user!r}")
    if leaks:
        sys.exit(f"refusing to write {name}: still contains {len(leaks)} private value(s)")

    target.mkdir(parents=True, exist_ok=True)
    for filename, body in files.items():
        (target / filename).write_text(body)
    print(f"scrubbed {target.relative_to(ROOT)} ({len(titles)} titles)")


def serial_map() -> dict[str, str]:
    """Every monitor serial seen in any raw recording -> a stable placeholder.

    Numbered by connector name, so the same monitor gets the same placeholder
    in every fixture, even one recorded while that monitor was unplugged and
    appears only in Omarchy's state files.
    """
    seen: dict[str, str] = {}  # serial -> connector it was first seen on
    for snapshot_file in sorted(RAW.glob("*/snapshot.json")):
        for mon in json.loads(snapshot_file.read_text())["monitors"]:
            if mon.get("serial"):
                seen.setdefault(mon["serial"], mon["name"])
    ordered = sorted(seen, key=lambda serial: (seen[serial], serial))
    return {serial: ("#" if serial.startswith("#") else "") + f"SERIAL{i}"
            for i, serial in enumerate(ordered, start=1)}


def _pacman_version(package: str) -> str | None:
    try:
        out = subprocess.run(["pacman", "-Q", package], capture_output=True, text=True, timeout=5)
        return out.stdout.split()[1] if out.returncode == 0 else None
    except (FileNotFoundError, IndexError, subprocess.TimeoutExpired):
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("record")
    p.add_argument("name")
    p.add_argument("--note", default="")
    p = sub.add_parser("scrub")
    p.add_argument("name")
    args = parser.parse_args()
    if args.command == "record":
        record(args.name, args.note)
    else:
        scrub(args.name)


if __name__ == "__main__":
    main()
