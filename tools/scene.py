"""Build or tear down a test scene of foot windows on empty workspaces.

    python3 tools/scene.py build      # open test windows (class snapback-test-*)
    python3 tools/scene.py teardown   # close only those windows
    python3 tools/scene.py show       # list test windows
    python3 tools/scene.py entries    # install hidden desktop entries that reopen them
    python3 tools/scene.py unentries  # remove those entries
    python3 tools/scene.py session F  # save a Snapback session of only the test windows to F
    python3 tools/scene.py shuffle    # move the test windows out of place

Only windows whose class starts with snapback-test- are ever touched.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

PREFIX = "snapback-test-"

# name, workspace, floating geometry as (dx, dy, w, h) relative to the monitor, or None for tiled
SCENE = [
    ("t1", 22, None),
    ("t2", 22, None),
    ("f1", 22, (200, 150, 900, 600)),
    ("f2", 12, (300, 400, 800, 1200)),
    ("f3", 2, (100, 120, 700, 450)),
    ("s1", "special:snapback-test", None),
]


def j(what):
    return json.loads(subprocess.run(["hyprctl", "-j", what], capture_output=True, text=True, check=True).stdout)


def lua(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value))  # JSON string escapes are valid Lua for plain ASCII


def dispatch(method, *positional, **args):
    body = ",".join(lua(v) for v in positional) if positional else \
        "{" + ",".join(f"{k}={lua(v)}" for k, v in args.items()) + "}"
    out = subprocess.run(["hyprctl", "eval", f"hl.dispatch(hl.dsp.{method}({body}))"],
                         capture_output=True, text=True, timeout=10)
    text = (out.stdout + out.stderr).strip()
    if text and text != "ok":
        print(f"  {method}: {text}")
    return text


def test_windows():
    return [c for c in j("clients") if c["class"].startswith(PREFIX)]


def wait_for(cls, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for c in j("clients"):
            if c["class"] == cls and c["mapped"]:
                return c
        time.sleep(0.1)
    raise SystemExit(f"{cls} never appeared")


def build():
    if test_windows():
        raise SystemExit("test windows already exist; run teardown first")
    home = json.loads(subprocess.run(["hyprctl", "-j", "activewindow"], capture_output=True, text=True).stdout or "{}")
    owners = {1: "HDMI-A-1", 2: "DP-2", 3: "DP-3"}  # Omarchy blocks 0,1,2 -> 1-10, 11-20, 21-30
    for name, ws, float_geom in SCENE:
        cls = PREFIX + name
        if isinstance(ws, int):
            # Focus the owning monitor, then the workspace, so it's created there.
            dispatch("focus", monitor=owners[(ws - 1) // 10 + 1])
            time.sleep(0.2)
            dispatch("focus", workspace=ws)
        else:
            dispatch("focus", workspace=22)
        time.sleep(0.3)
        dispatch("exec_cmd", f"foot --app-id={cls} --title=snapback-{name} sleep infinity")
        c = wait_for(cls)
        sel = f"address:{c['address']}"
        if isinstance(ws, str):
            dispatch("window.move", window=sel, workspace=ws, follow=False)
        time.sleep(0.3)
        if float_geom:
            dx, dy, w, h = float_geom
            dispatch("window.float", window=sel, action="on")
            time.sleep(0.2)
            c = next(x for x in j("clients") if x["address"] == c["address"])
            mon = next(m for m in j("monitors") if m["id"] == c["monitor"])
            dispatch("window.resize", window=sel, x=w, y=h, exact=True)
            time.sleep(0.2)
            dispatch("window.move", window=sel, x=mon["x"] + dx, y=mon["y"] + dy, exact=True)
            time.sleep(0.2)
        print(f"  {cls} -> ws {ws}" + (f" floating {float_geom}" if float_geom else " tiled"))
    if home.get("address"):
        dispatch("focus", window=f"address:{home['address']}")


def teardown():
    for c in test_windows():
        dispatch("window.close", window=f"address:{c['address']}")
    deadline = time.monotonic() + 5
    while test_windows() and time.monotonic() < deadline:
        time.sleep(0.1)
    left = test_windows()
    if left:
        subprocess.run(["pkill", "-f", f"app-id={PREFIX}"])
    print(f"  closed; {len(test_windows())} test windows left")


def entries_dir():
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")
    return Path(base) / "applications"


def entries():
    """Hidden entries (NoDisplay) so the launcher never lists them, one per test window."""
    entries_dir().mkdir(parents=True, exist_ok=True)
    for name, _, _ in SCENE:
        cls = PREFIX + name
        (entries_dir() / f"{cls}.desktop").write_text(
            "[Desktop Entry]\nType=Application\nName=Snapback test " + name + "\n"
            f"Exec=foot --app-id={cls} --title=snapback-{name} sleep infinity\n"
            f"StartupWMClass={cls}\nNoDisplay=true\n")
    print(f"  wrote {len(SCENE)} entries to {entries_dir()}")


def unentries():
    removed = 0
    for path in entries_dir().glob(PREFIX + "*.desktop"):
        path.unlink()
        removed += 1
    print(f"  removed {removed} entries")


def session(path):
    repo = Path(__file__).resolve().parent.parent
    out = subprocess.run([sys.executable, "-m", "snapback", "save", "--dry-run", "--json"],
                         cwd=repo, capture_output=True, text=True, check=True).stdout
    saved = json.loads(out)
    saved["windows"] = [w for w in saved["windows"] if w["app"].startswith(PREFIX)]
    Path(path).write_text(json.dumps(saved, indent=2))
    os.chmod(path, 0o600)
    ok = sum(1 for w in saved["windows"] if w["launch"])
    print(f"  saved {len(saved['windows'])} test windows ({ok} reopenable) to {path}")


def shuffle():
    """Tiled t1 -> ws 12, floating f1 tiled on ws 22, f2 nudged, f3 -> ws 22, s1 -> ws 22."""
    windows = {c["class"]: c for c in test_windows()}
    sel = lambda n: f"address:{windows[PREFIX + n]['address']}"  # noqa: E731
    dispatch("window.move", window=sel("t1"), workspace="12", follow=False)
    dispatch("window.float", window=sel("f1"), action="off")
    f2 = windows[PREFIX + "f2"]
    dispatch("window.move", window=sel("f2"), x=f2["at"][0] + 150, y=f2["at"][1] + 200, exact=True)
    dispatch("window.move", window=sel("f3"), workspace="22", follow=False)
    dispatch("window.move", window=sel("s1"), workspace="22", follow=False)
    time.sleep(0.3)
    show()


def show():
    for c in test_windows():
        print(f"  {c['class']:<22} ws {c['workspace']['name']:<22} float={c['floating']} at {c['at']} size {c['size']}")


if __name__ == "__main__":
    commands = {"build": build, "teardown": teardown, "show": show, "entries": entries,
                "unentries": unentries, "shuffle": shuffle, "session": session}
    commands[sys.argv[1]](*sys.argv[2:])
