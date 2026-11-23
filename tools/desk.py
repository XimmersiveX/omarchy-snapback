"""Snapshot the desk before a live test and check it's back afterwards.

    python3 tools/desk.py save FILE    # windows, monitors, workspaces, Omarchy state
    python3 tools/desk.py check FILE   # prints IDENTICAL, or what differs

Read-only. Keep FILE outside the repo (e.g. the session scratchpad): it holds
window classes and the machine's monitor serials.
"""

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from snapback.omarchy import state_files  # noqa: E402


def j(what):
    return json.loads(subprocess.run(["hyprctl", "-j", *what.split()], capture_output=True, text=True).stdout)


def snapshot():
    return {
        "clients": sorted([c["address"], c["class"], c["workspace"]["id"], c["monitor"], c["at"], c["size"],
                           c["floating"]] for c in j("clients")),
        "monitors": sorted([m["name"], m["x"], m["y"], m["width"], m["height"], m["scale"], m["transform"],
                            m["activeWorkspace"]["id"], m["disabled"]] for m in j("monitors all")),
        "workspaces": sorted([w["id"], w["monitor"]] for w in j("workspaces")),
        "state": [p.read_text() if p.exists() else None for p in state_files()],
    }


def main():
    action, path = sys.argv[1], Path(sys.argv[2])
    now = snapshot()
    if action == "save":
        path.write_text(json.dumps(now, indent=1))
        print(f"saved {len(now['clients'])} windows")
        return 0
    before = json.loads(path.read_text())
    diff = [k for k in before if before[k] != now[k]]
    if not diff:
        print("IDENTICAL")
        return 0
    print(f"DIFFERS in {', '.join(diff)}")
    for k in diff:
        print(f"{k}\n  before: {before[k]}\n  after:  {now[k]}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
