"""Running a restore against a fake desktop that records every call."""

import copy
import unittest

from snapback import hypr, plan, restore
from tests.test_plan import ENTRIES, desk, empty, saved_scene


GAPS_OUT, GAPS_IN = 12, 7


class FakeDesk:
    """Enough of Hyprland to run a restore: windows, workspaces, launches that
    open a window a few polls later, a clock that moves on each sleep, and a
    small dwindle: each workspace's tiled windows form a tree of splits, a new
    tile splits the focused window (or the workspace's last-focused one) on
    the preselected side, or the long side, and tiles are laid out again
    whenever a workspace's tree changes. Workspaces nobody changes keep the
    positions they were recorded with."""

    def __init__(self, source, opens=None, delay=2, fail=None, opens_on="DP-3"):
        snap = copy.deepcopy(source.snapshot)
        self._clients = snap["clients"]
        self._workspaces = snap["workspaces"]
        self.names = {m["id"]: m["name"] for m in snap["monitors"]}
        self._monitors = snap["monitors"]
        self.opens_on = next(i for i, name in self.names.items() if name == opens_on)
        self.focused = next((c["address"] for c in self._clients if c.get("focusHistoryID") == 0), None)
        self.focused_monitor = next((m["name"] for m in self._monitors if m.get("focused")), opens_on)
        self.opens = opens or {}        # command[-1] -> list of (class, title) it opens
        self.delay = delay              # polls before a launched app's windows appear
        self.fail = fail or set()       # dispatch methods that error
        self.stubborn = set()           # addresses that ask before closing, so stay open
        self.slow = {}                  # address -> polls it takes to close
        self.closing = []               # (time, address)
        self.calls = []
        self.launched = []
        self.option = True
        self.failsafe = None
        self.clock = 0.0
        self.queue = []                 # (time, client)
        self.next_id = 0xa000
        self.preselect = None
        self.last_focused = {}          # workspace name -> address
        self.trees = {}                 # workspace name -> tree, built on first change
        if self.focused:
            self.last_focused[self._by("address:" + self.focused)["workspace"]["name"]] = self.focused

    # Desk interface
    def clients(self):
        for when, address in list(self.closing):
            if when <= self.clock:
                self.closing.remove((when, address))
                client = self._by("address:" + address)
                self._remove(client)
                self._clients.remove(client)
        while self.queue and self.queue[0][0] <= self.clock:
            client = self.queue.pop(0)[1]
            self._clients.append(client)
            self._insert(client)
        return copy.deepcopy(self._clients)

    def workspaces(self):
        return copy.deepcopy(self._workspaces)

    def monitors(self):
        return copy.deepcopy(self._monitors)

    def active_window(self):
        return self.focused

    def layout(self, message):
        self.calls.append(("layout", {"message": message}))
        self.preselect = message.split()[1]

    def dispatch(self, method, **args):
        self.calls.append((method, args))
        if method in self.fail:
            raise hypr.HyprError(f"{method} refused")
        if method == "window.move" and "workspace" in args:
            client = self._by(args["window"])
            name = args["workspace"]
            self._remove(client)
            client["workspace"] = {"id": int(name) if name.isdigit() else -98, "name": name}
            if name.isdigit() and not any(w["id"] == int(name) for w in self._workspaces):
                self._workspaces.append({"id": int(name), "name": name,
                                         "monitor": self.names[client["monitor"]], "windows": 1})
            self._insert(client)
        elif method == "window.close":
            client = self._by(args["window"])
            if client["address"] in self.slow:
                self.closing.append((self.clock + self.slow[client["address"]], client["address"]))
            elif client["address"] not in self.stubborn:
                self._remove(client)
                self._clients.remove(client)
        elif method == "workspace.move":
            for w in self._workspaces:
                if w["name"] == args["workspace"]:
                    w["monitor"] = args["monitor"]
        elif method == "window.float":
            client = self._by(args["window"])
            if client["floating"] != (args["action"] == "on"):
                (self._remove if args["action"] == "on" else lambda c: None)(client)
                client["floating"] = args["action"] == "on"
                if not client["floating"]:
                    self._insert(client)
        elif method == "window.resize":
            client = self._by(args["window"])
            if client["floating"]:
                client["size"] = [args["x"], args["y"]]
        elif method == "window.move":
            self._by(args["window"])["at"] = [args["x"], args["y"]]
        elif method == "window.swap":
            a, b = self._by(args["window"]), self._by(args["target"])
            for key in ("at", "size", "workspace", "monitor"):
                a[key], b[key] = b[key], a[key]
        elif method == "focus" and "window" in args:
            self.focused = args["window"].split(":", 1)[1]
            client = self._by(args["window"])
            ws = client["workspace"]["name"]
            self.last_focused[ws] = self.focused
            if ws.isdigit():
                self._show(self._ws_monitor(ws), ws)
        elif method == "focus" and "monitor" in args:
            self.focused_monitor = args["monitor"]
        elif method == "focus" and "workspace" in args:
            ws = args["workspace"]
            if not any(w["name"] == ws for w in self._workspaces):
                self._workspaces.append({"id": int(ws), "name": ws, "monitor": self.focused_monitor, "windows": 0})
            self._show(self._ws_monitor(ws), ws)

    def launch(self, command):
        self.launched.append(command)
        monitor = next(m for m in self._monitors if m["id"] == self.opens_on)
        ws = monitor["activeWorkspace"]
        for cls, title in self.opens.get(command[-1], []):
            self.next_id += 1
            self.queue.append((self.clock + self.delay, {
                "address": hex(self.next_id), "class": cls, "initialClass": cls, "title": title,
                "mapped": True, "hidden": False, "floating": False, "monitor": self.opens_on,
                "workspace": dict(ws), "at": [0, 0], "size": [100, 100],
                "pid": 1, "fullscreen": 0, "pinned": False, "grouped": [], "focusHistoryID": 5}))

    def focus_on_activate(self):
        return self.option

    def set_focus_on_activate(self, on, failsafe_ms=None):
        self.option = on
        self.failsafe = failsafe_ms

    def sleep(self, seconds):
        self.clock += 1

    def now(self):
        return self.clock

    # The small dwindle
    def _by(self, selector):
        address = selector.split(":", 1)[1]
        return next(c for c in self._clients if c["address"] == address)

    def _ws_monitor(self, ws):
        return next((w["monitor"] for w in self._workspaces if w["name"] == ws), self.focused_monitor)

    def _show(self, monitor, ws):
        for m in self._monitors:
            if m["name"] == monitor:
                m["activeWorkspace"] = {"id": int(ws), "name": ws}
        self.focused_monitor = monitor

    def _tiles(self, ws):
        return [c for c in self._clients if c["workspace"]["name"] == ws and not c["floating"]]

    def _tree(self, ws):
        if ws not in self.trees:
            tree = None
            for c in sorted(self._tiles(ws), key=lambda c: (c["at"][0], c["at"][1])):
                tree = c["address"] if tree is None else ("r", tree, c["address"])
            self.trees[ws] = tree
        return self.trees[ws]

    def _insert(self, client):
        if client["floating"]:
            return
        ws = client["workspace"]["name"]
        tree = self._tree(ws)
        new = client["address"]
        if tree is None:
            self.trees[ws] = new
        else:
            leaves = _leaves(tree)
            focused = self.focused if self.focused in leaves else None
            target = focused or (self.last_focused.get(ws) if self.last_focused.get(ws) in leaves else leaves[0])
            side = self.preselect or "r"
            self.preselect = None
            self.trees[ws] = _replace(tree, target, (side, target, new))
        self._lay_out(ws)

    def _remove(self, client):
        ws = client["workspace"]["name"]
        if client["floating"] or self._tree(ws) is None:
            return
        self.trees[ws] = _without(self.trees[ws], client["address"])
        self._lay_out(ws)

    def _lay_out(self, ws):
        if not ws.isdigit() or self.trees.get(ws) is None:
            return
        m = next(m for m in self._monitors if m["name"] == self._ws_monitor(ws))
        w, h = m["width"] / m["scale"], m["height"] / m["scale"]
        if m.get("transform", 0) % 2:
            w, h = h, w
        top = m["reserved"][1]
        _place(self.trees[ws], (m["x"] + GAPS_OUT, m["y"] + top + GAPS_OUT,
                                w - 2 * GAPS_OUT, h - top - 2 * GAPS_OUT),
               {c["address"]: c for c in self._clients}, m["id"])


def _leaves(tree):
    return _leaves(tree[1]) + _leaves(tree[2]) if isinstance(tree, tuple) else [tree]


def _replace(tree, leaf, new):
    if isinstance(tree, tuple):
        return (tree[0], _replace(tree[1], leaf, new), _replace(tree[2], leaf, new))
    return new if tree == leaf else tree


def _without(tree, leaf):
    if not isinstance(tree, tuple):
        return None if tree == leaf else tree
    a, b = _without(tree[1], leaf), _without(tree[2], leaf)
    if a is None:
        return b
    if b is None:
        return a
    return (tree[0], a, b)


def _place(tree, box, clients, monitor):
    x, y, w, h = box
    if not isinstance(tree, tuple):
        clients[tree]["at"], clients[tree]["size"] = [round(x), round(y)], [round(w), round(h)]
        clients[tree]["monitor"] = monitor
        return
    side, a, b = tree
    if side == "r":
        half = (w - 2 * GAPS_IN) / 2
        _place(a, (x, y, half, h), clients, monitor)
        _place(b, (x + half + 2 * GAPS_IN, y, half, h), clients, monitor)
    else:
        half = (h - 2 * GAPS_IN) / 2
        _place(a, (x, y, w, half), clients, monitor)
        _place(b, (x, y + half + 2 * GAPS_IN, w, half), clients, monitor)


def make_plan(source):
    return plan.build(saved_scene(), source, ENTRIES, default_terminal="foot.desktop",
                      is_dir=lambda path: True)


OPENS = {
    "chromium.desktop": [("chromium", "Chromium")],
    "t3code.desktop": [("com.t3tools.T3Code", "T3 Code")],
    "--dir=/home/me/work": [("snapback-test-t1", "foot")],
}


class Moves(unittest.TestCase):
    def test_nothing_to_do_changes_nothing(self):
        source = desk("tv-on-scene")
        fake = FakeDesk(source)
        report = restore.run(make_plan(source), fake)
        self.assertEqual(report.count("in-place"), 9)
        self.assertEqual([c[0] for c in fake.calls], ["focus"])  # focus handed back, nothing else
        self.assertTrue(fake.option)

    def test_open_window_is_moved_silently_into_place(self):
        source = desk("tv-unplugged")
        fake = FakeDesk(source)
        report = restore.run(make_plan(source), fake)
        t3 = next(r for r in report.results if r.step.app == "com.t3tools.T3Code")
        self.assertEqual(t3.outcome, "placed")
        move = next(args for m, args in fake.calls if m == "window.move" and "workspace" in args)
        self.assertEqual(move, {"window": f"address:{t3.address}", "workspace": "13", "follow": False})


    def test_floating_window_is_floated_sized_and_put_back(self):
        source = desk("tv-on-scene")
        f1 = next(c for c in source.snapshot["clients"] if c["initialClass"] == "snapback-test-f1")
        f1["floating"], f1["at"], f1["size"] = False, [3852, 788], [1261, 1390]
        fake = FakeDesk(source)
        restore.run(make_plan(source), fake)
        window = f"address:{f1['address']}"
        self.assertIn(("window.float", {"window": window, "action": "on"}), fake.calls)
        after = fake._by(window)
        self.assertEqual((after["floating"], after["at"], after["size"]), (True, [4040, 900], [900, 600]))

    def test_workspace_on_the_wrong_screen_is_moved_first(self):
        source = desk("tv-on-scene")
        next(w for w in source.snapshot["workspaces"] if w["id"] == 12)["monitor"] = "HDMI-A-1"
        fake = FakeDesk(source)
        restore.run(make_plan(source), fake)
        self.assertEqual(fake.calls[0], ("workspace.move", {"workspace": "12", "monitor": "DP-2"}))

    def test_focus_falls_back_to_the_sessions_last_used_window(self):
        source = desk("tv-on-scene")
        fake = FakeDesk(source)
        fake.focused = None
        p = make_plan(source)
        restore.run(p, fake)
        self.assertEqual(fake.focused, p.windows[0].window)


class TileOrder(unittest.TestCase):
    """t1 was saved on the left of ws 22 and t2 on the right."""

    def swapped(self):
        source = desk("tv-on-scene")
        t1, t2 = (next(c for c in source.snapshot["clients"] if c["initialClass"] == f"snapback-test-t{n}")
                  for n in (1, 2))
        t1["at"], t2["at"] = t2["at"], t1["at"]
        return source, t1, t2

    def test_swapped_tiles_are_swapped_back(self):
        source, t1, t2 = self.swapped()
        fake = FakeDesk(source)
        report = restore.run(make_plan(source), fake)
        self.assertEqual(report.reordered, ["22"])
        swaps = [args for m, args in fake.calls if m == "window.swap"]
        self.assertEqual(len(swaps), 1)
        self.assertEqual({swaps[0]["window"], swaps[0]["target"]},
                         {f"address:{t1['address']}", f"address:{t2['address']}"})
        self.assertLess(fake._by(f"address:{t1['address']}")["at"][0],
                        fake._by(f"address:{t2['address']}")["at"][0])

    def test_tiles_in_order_are_left_alone(self):
        source = desk("tv-on-scene")
        fake = FakeDesk(source)
        report = restore.run(make_plan(source), fake)
        self.assertEqual(report.reordered, [])
        self.assertFalse(any(m == "window.swap" for m, _ in fake.calls))

    def test_floating_windows_are_not_tiles(self):
        source, t1, t2 = self.swapped()
        p = make_plan(source)
        for step in p.windows:
            if step.app == "snapback-test-f1":
                self.assertIsNone(step.tile)

    def test_stale_positions_on_a_moved_hidden_workspace(self):
        # ws 22 moved to the portrait ROG while hidden: Hyprland still reports
        # its tiles where the main ROG laid them out, here swapped.
        source, t1, t2 = self.swapped()
        portrait = next(m["id"] for m in source.snapshot["monitors"] if m["name"] == "DP-2")
        for c in (t1, t2):
            c["monitor"] = portrait
        fake = FakeDesk(source)
        restore.run(make_plan(source), fake)
        self.assertEqual(len([m for m, _ in fake.calls if m == "window.swap"]), 1)

    def test_a_refused_swap_is_reported(self):
        source, _, _ = self.swapped()
        report = restore.run(make_plan(source), FakeDesk(source, fail={"window.swap"}))
        self.assertTrue(any("reorder" in e for e in report.errors))


class Swaps(unittest.TestCase):
    def test_tile_box_of_a_stale_window_is_measured_where_it_was_laid_out(self):
        from snapback.geometry import Rect
        main, portrait = Rect(3840, 750, 2560, 1440), Rect(6400, 0, 1440, 2560)
        client = {"at": [3852, 788], "size": [1261, 1390]}  # left tile of the main ROG
        x0, y0, x1, y1 = restore._tile_box(client, portrait, [main, portrait])
        self.assertAlmostEqual(x0, 12 / 2560)
        self.assertAlmostEqual(y1, (38 + 1390) / 1440)

    def test_permutations(self):
        # Three tiles in a row; the windows sit in them rotated one step.
        tiles = [(0.2, 0.5), (0.5, 0.5), (0.8, 0.5)]
        saved = [tiles[1], tiles[2], tiles[0]]      # window i belongs in tile (i+1)%3
        swaps = restore._swaps(saved, tiles)
        where = [0, 1, 2]
        for i, j in swaps:
            where[i], where[j] = where[j], where[i]
        self.assertEqual(where, [1, 2, 0])
        self.assertEqual(len(swaps), 2)

    def test_already_best_needs_no_swaps(self):
        tiles = [(0.25, 0.5), (0.75, 0.5)]
        self.assertEqual(restore._swaps(tiles, tiles), [])

    def test_many_windows_use_the_greedy_path(self):
        tiles = [(i / 10, 0.5) for i in range(9)]
        saved = list(reversed(tiles))
        swaps = restore._swaps(saved, tiles)
        where = list(range(9))
        for i, j in swaps:
            where[i], where[j] = where[j], where[i]
        self.assertEqual(where, list(reversed(range(9))))


class Launches(unittest.TestCase):
    def setUp(self):
        self.source = empty(desk("tv-on-scene"))
        self.fake = FakeDesk(self.source, OPENS)
        self.report = restore.run(make_plan(self.source), self.fake, timeout=10)
        self.by_app = {r.step.app: r for r in self.report.results}

    def test_each_app_is_launched_once_and_its_window_placed(self):
        self.assertEqual(len(self.fake.launched), 3)
        for app in ("chromium", "com.t3tools.T3Code", "snapback-test-t1"):
            self.assertEqual(self.by_app[app].outcome, "placed", app)
        t1 = next(c for c in self.fake._clients if c["class"] == "snapback-test-t1")
        self.assertEqual(t1["workspace"]["name"], "22")

    def test_new_workspace_is_moved_to_its_owner(self):
        # Chromium saved on the TV's slot 2, which doesn't exist after a reboot.
        # The fake opens new windows on the main ROG, so moving the window there
        # makes ws 2 on the wrong screen, and it has to go to the TV.
        session = saved_scene()
        next(w for w in session["windows"] if w["app"] == "chromium")["slot"] = 2
        p = plan.build(session, self.source, ENTRIES, default_terminal="foot.desktop",
                       is_dir=lambda path: True)
        fake = FakeDesk(self.source, OPENS)
        restore.run(p, fake, timeout=10)
        ws2 = next(w for w in fake._workspaces if w["id"] == 2)
        self.assertEqual(ws2["monitor"], "HDMI-A-1")
        self.assertIn(("workspace.move", {"workspace": "2", "monitor": "HDMI-A-1"}), fake.calls)

    def test_a_window_open_before_the_launch_is_never_taken_for_the_new_one(self):
        source = desk("tv-on-scene")
        session = saved_scene()
        second = copy.deepcopy(next(w for w in session["windows"] if w["app"] == "chromium"))
        second["title"] = "Chromium"  # the title the fake's new window has
        session["windows"].append(second)
        p = plan.build(session, source, ENTRIES)
        fake = FakeDesk(source, OPENS)
        report = restore.run(p, fake, timeout=10)
        launched = next(r for r in report.results if r.step.action == "launch")
        old = {c["address"] for c in source.snapshot["clients"]}
        self.assertEqual(launched.outcome, "placed")
        self.assertNotIn(launched.address, old)

    def test_unresolved_windows_are_reported_as_unrestorable(self):
        self.assertEqual(self.by_app["snapback-test-f1"].outcome, "unrestorable")

    def test_focus_on_activate_is_off_during_and_on_after(self):
        self.assertTrue(self.fake.option)
        self.assertEqual(self.fake.failsafe, None)  # the last call turned it on, no timer
        self.assertIn(("set", False), _option_history(self.source))


def _option_history(source):
    """Run once more, recording each focus_on_activate change and launch in order."""
    fake = FakeDesk(source, OPENS)
    history = []
    real_set, real_launch = fake.set_focus_on_activate, fake.launch

    def set_option(on, failsafe_ms=None):
        history.append(("set", on))
        real_set(on, failsafe_ms)

    def launch(command):
        history.append(("launch", fake.option))
        real_launch(command)

    fake.set_focus_on_activate, fake.launch = set_option, launch
    restore.run(make_plan(source), fake, timeout=10)
    assert history[0] == ("set", False) and history[-1] == ("set", True), history
    assert all(on is False for kind, on in history if kind == "launch"), history
    return history


class Failures(unittest.TestCase):
    def test_app_that_never_opens_a_window_times_out(self):
        source = empty(desk("tv-on-scene"))
        opens = dict(OPENS, **{"t3code.desktop": []})
        report = restore.run(make_plan(source), FakeDesk(source, opens), timeout=5)
        t3 = next(r for r in report.results if r.step.app == "com.t3tools.T3Code")
        self.assertEqual(t3.outcome, "failed")
        self.assertIn("within 5s", t3.detail)

    def test_extra_windows_are_left_alone_and_reported(self):
        source = empty(desk("tv-on-scene"))
        opens = dict(OPENS, **{"chromium.desktop": [("chromium", "Chromium"), ("chromium", "Old tab")]})
        fake = FakeDesk(source, opens)
        report = restore.run(make_plan(source), fake, timeout=10)
        self.assertEqual(len(report.extra), 1)
        extra = report.extra[0].split("(")[1].rstrip(")")
        self.assertFalse(any(args.get("window") == f"address:{extra}" for _, args in fake.calls))

    def test_title_picks_the_right_window(self):
        source = empty(desk("tv-on-scene"))
        session = saved_scene()
        chromium = next(w for w in session["windows"] if w["app"] == "chromium")
        chromium["title"] = "Mail"
        p = plan.build(session, source, ENTRIES, default_terminal="foot.desktop", is_dir=lambda p: True)
        opens = dict(OPENS, **{"chromium.desktop": [("chromium", "News"), ("chromium", "Mail")]})
        fake = FakeDesk(source, opens)
        report = restore.run(p, fake, timeout=10)
        placed = next(r for r in report.results if r.step.app == "chromium")
        self.assertEqual(next(c for c in fake._clients if c["address"] == placed.address)["title"], "Mail")

    def test_a_refused_dispatch_fails_that_window_only_and_still_cleans_up(self):
        source = desk("tv-unplugged")
        fake = FakeDesk(source, fail={"window.move"})
        report = restore.run(make_plan(source), fake)
        self.assertEqual(report.count("failed"), 1)
        self.assertTrue(fake.option)

    def test_crash_mid_restore_still_turns_focus_on_activate_back_on(self):
        source = empty(desk("tv-on-scene"))
        fake = FakeDesk(source, OPENS)

        def boom(command):
            raise KeyboardInterrupt

        fake.launch = boom
        with self.assertRaises(KeyboardInterrupt):
            restore.run(make_plan(source), fake)
        self.assertTrue(fake.option)

    def test_option_already_off_is_left_alone(self):
        source = empty(desk("tv-on-scene"))
        fake = FakeDesk(source, OPENS)
        fake.option = False
        changes = []
        fake.set_focus_on_activate = lambda on, failsafe_ms=None: changes.append(on)
        restore.run(make_plan(source), fake, timeout=10)
        self.assertEqual(changes, [])

    def test_focus_goes_back_to_the_window_that_had_it(self):
        source = desk("tv-unplugged")
        fake = FakeDesk(source)
        p = make_plan(source)
        before = fake.focused = next(w.window for w in p.windows if w.app == "snapback-test-t2")
        self.assertNotEqual(before, p.windows[0].window)  # not the session's last-used one
        restore.run(p, fake)
        self.assertEqual(fake.calls[-1], ("focus", {"window": f"address:{before}"}))

    def test_focus_never_pulls_a_screen_off_the_restored_view(self):
        # Chromium is on ws 13, but the session's view for DP-2 is ws 12.
        source = desk("tv-unplugged")
        fake = FakeDesk(source)
        p = make_plan(source)
        hidden = fake.focused = next(w.window for w in p.windows if w.app == "chromium")
        restore.run(p, fake)
        shown = {m["activeWorkspace"]["name"] for m in fake.monitors()}
        self.assertEqual(shown, {"12", "22"})
        last = [c for c in fake.calls if c[0] == "focus" and "window" in c[1]][-1]
        self.assertNotEqual(last[1]["window"], f"address:{hidden}")



class SwitchTo(unittest.TestCase):
    def setUp(self):
        self.source = desk("tv-on-scene")
        session = saved_scene()
        session["windows"] = [w for w in session["windows"] if w["app"] == "chromium"]
        self.plan = plan.build(session, self.source, ENTRIES, default_terminal="foot.desktop",
                               is_dir=lambda path: True)
        self.fake = FakeDesk(self.source)

    def test_closes_the_windows_the_session_does_not_name_first(self):
        report = restore.run(self.plan, self.fake, close_others=True)
        left = {c["address"] for c in self.fake.clients()}
        for other in self.plan.others:
            self.assertNotIn(other["address"], left)
        self.assertEqual(sorted(report.closed), sorted(o["name"] for o in self.plan.others))
        closes = [i for i, c in enumerate(self.fake.calls) if c[0] == "window.close"]
        first_other = next(i for i, c in enumerate(self.fake.calls) if c[0] != "window.close")
        self.assertLess(max(closes), first_other)
        self.assertIn("chromium", {c["initialClass"] for c in self.fake.clients()})

    def test_a_window_that_asks_first_stays_and_is_reported(self):
        stubborn = self.plan.others[0]
        self.fake.stubborn.add(stubborn["address"])
        report = restore.run(self.plan, self.fake, close_others=True)
        self.assertEqual(report.kept, [stubborn["name"]])
        self.assertIn(stubborn["address"], {c["address"] for c in self.fake.clients()})
        self.assertEqual(len(report.closed), len(self.plan.others) - 1)

    def test_waits_for_an_app_that_takes_a_moment_to_close(self):
        slow = self.plan.others[0]
        self.fake.slow[slow["address"]] = 2
        report = restore.run(self.plan, self.fake, close_others=True)
        self.assertEqual(report.kept, [])
        self.assertEqual(len(report.closed), len(self.plan.others))

    def test_nothing_is_closed_unless_asked(self):
        report = restore.run(self.plan, self.fake)
        self.assertFalse(any(c[0] == "window.close" for c in self.fake.calls))
        self.assertEqual((report.closed, report.kept), ([], []))

    def test_a_refused_close_is_an_error_not_a_crash(self):
        self.fake.fail.add("window.close")
        report = restore.run(self.plan, self.fake, close_others=True)
        self.assertEqual(len(report.errors), len(self.plan.others))
        self.assertEqual(report.closed, [])


class Views(unittest.TestCase):
    def test_each_screen_shows_the_workspace_it_showed_when_saved(self):
        source = desk("tv-on-scene")
        fake = FakeDesk(source)
        for m in fake._monitors:  # someone looked at other workspaces since
            m["activeWorkspace"] = {"id": 15, "name": "15"} if m["name"] == "DP-2" else m["activeWorkspace"]
        fake._workspaces.append({"id": 15, "name": "15", "monitor": "DP-2", "windows": 0})
        restore.run(make_plan(source), fake)
        self.assertEqual({m["name"]: m["activeWorkspace"]["name"] for m in fake.monitors()},
                         {"DP-2": "12", "DP-3": "22", "HDMI-A-1": "1"})


class LockTest(unittest.TestCase):
    def test_second_restore_is_refused(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lock"
            with restore.Lock(path):
                with self.assertRaises(restore.RestoreError):
                    with restore.Lock(path):
                        pass
            with restore.Lock(path):
                pass


class LuaQuoting(unittest.TestCase):
    def test_values(self):
        self.assertEqual(hypr.lua({"a": 1, "b": True, "c": "x\"y\\z\n"}), '{a=1,b=true,c="x\\"y\\\\z\\010"}')


if __name__ == "__main__":
    unittest.main()
