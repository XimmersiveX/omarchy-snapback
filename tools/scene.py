"""Build or tear down a test scene of foot windows on empty workspaces.

    python3 tools/scene.py build      # open test windows (class snapback-test-*)
    python3 tools/scene.py teardown   # close only those windows
    python3 tools/scene.py show       # list test windows

Only windows whose class starts with snapback-test- are ever touched.
"""

import json
import subprocess
import sys
import time

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


def show():
    for c in test_windows():
        print(f"  {c['class']:<22} ws {c['workspace']['name']:<22} float={c['floating']} at {c['at']} size {c['size']}")


if __name__ == "__main__":
    {"build": build, "teardown": teardown, "show": show}[sys.argv[1]]()
