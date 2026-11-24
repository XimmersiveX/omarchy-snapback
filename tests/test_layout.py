"""Tiled layouts: reading the tree of splits, and rebuilding it in a restore."""

import copy
import unittest

from snapback import hypr, layout, plan, restore
from snapback.apps import DesktopEntry
from snapback.layout import Split
from tests.test_plan import MAIN, desk, empty, saved_scene
from tests.test_restore import FakeDesk

# The user's TV layout, on the main ROG: A above (B | C) on the left, D on the right.
# Boxes are what dwindle makes there: 12 px outer gaps, 14 px between tiles, a 26 px bar.
TILES = {
    "a": (12, 38, 1261, 688),
    "b": (12, 740, 623.5, 688),
    "c": (649.5, 740, 623.5, 688),
    "d": (1287, 38, 1261, 1390),
}


def frac(dx, dy, w, h, mw=2560, mh=1440):
    return (dx / mw, dy / mh, (dx + w) / mw, (dy + h) / mh)


class SplitTree(unittest.TestCase):
    def test_the_users_layout(self):
        tree = layout.split_tree({k: frac(*v) for k, v in TILES.items()})
        self.assertEqual(tree, Split(True, Split(False, "a", Split(True, "b", "c")), "d"))

    def test_build_steps(self):
        tree = Split(True, Split(False, "a", Split(True, "b", "c")), "d")
        self.assertEqual(layout.build_steps(tree), ("a", [("a", "r", "d"), ("a", "d", "b"), ("b", "r", "c")]))
        self.assertEqual(layout.leaves(tree), ["a", "b", "c", "d"])

    def test_three_columns_cut_nearest_the_middle(self):
        # Dwindle makes x | (y | z): x is half the width.
        boxes = {"x": (0, 0, 0.5, 1), "y": (0.5, 0, 0.75, 1), "z": (0.75, 0, 1, 1)}
        self.assertEqual(layout.split_tree(boxes), Split(True, "x", Split(True, "y", "z")))

    def test_the_middle_cut_wins_even_when_found_later(self):
        # (x | y) | z with z half the width: the cut at 0.25 comes first, 0.5 is right.
        boxes = {"x": (0, 0, 0.25, 1), "y": (0.25, 0, 0.5, 1), "z": (0.5, 0, 1, 1)}
        self.assertEqual(layout.split_tree(boxes), Split(True, Split(True, "x", "y"), "z"))

    def test_a_pinwheel_is_no_tree_of_splits(self):
        boxes = {"n": (0, 0, 0.66, 0.33), "e": (0.66, 0, 1, 0.66), "s": (0.33, 0.66, 1, 1),
                 "w": (0, 0.33, 0.33, 1), "c": (0.33, 0.33, 0.66, 0.66)}
        self.assertIsNone(layout.split_tree(boxes))

    def test_one_window_and_none(self):
        self.assertEqual(layout.split_tree({"x": (0, 0, 1, 1)}), "x")
        self.assertIsNone(layout.split_tree({}))

    def test_same_shape(self):
        a = {"x": (0, 0, 0.5, 1), "y": (0.5, 0, 1, 1)}
        self.assertTrue(layout.same_shape(a, {"x": (0.01, 0, 0.5, 1), "y": (0.5, 0, 1, 1)}))
        self.assertFalse(layout.same_shape(a, {"x": (0.5, 0, 1, 1), "y": (0, 0, 0.5, 1)}))
        self.assertFalse(layout.same_shape(a, {"x": (0, 0, 0.5, 1)}))


def entries():
    return {f"{k}.desktop": DesktopEntry(f"{k}.desktop", k, k, k, False, False) for k in TILES}


OPENS = {f"{k}.desktop": [(k, k.upper())] for k in TILES}


def tv_layout_session(order="dbac", workspace="23"):
    """A session holding the user's layout on the main ROG's slot 3, saved in `order`."""
    session = saved_scene()
    template = next(w for w in session["windows"] if w["app"] == "snapback-test-t1")
    windows = []
    for k in order:
        dx, dy, w, h = TILES[k]
        window = copy.deepcopy(template)
        window.update(app=k, title=k.upper(), workspace=workspace, monitor=MAIN, slot=(int(workspace) - 1) % 10 + 1,
                      floating=False, stable_id=None, skip=None, problem=None,
                      launch={"how": "desktop", "entry": f"{k}.desktop", "via": "wm-class"},
                      placement={"dx": dx, "dy": dy, "w": w, "h": h, "monitor_w": 2560, "monitor_h": 1440})
        windows.append(window)
    session["windows"] = windows
    return session


def tiles_now(fake, workspace):
    main = next(m for m in fake.monitors() if m["name"] == "DP-3")
    return {c["class"]: frac(c["at"][0] - main["x"], c["at"][1] - main["y"], *c["size"])
            for c in fake.clients() if c["workspace"]["name"] == workspace and not c["floating"]}


class Rebuild(unittest.TestCase):
    def run_restore(self, fake=None, session=None):
        source = empty(desk("tv-on-scene"))
        p = plan.build(session or tv_layout_session(), source, entries())
        fake = fake or FakeDesk(source, OPENS)
        return restore.run(p, fake, timeout=20), fake

    def test_apps_arriving_in_any_order_get_the_saved_layout(self):
        report, fake = self.run_restore()
        self.assertEqual(report.count("placed"), 4)
        self.assertEqual(report.rebuilt, ["23"])
        saved = {k: frac(*v) for k, v in TILES.items()}
        self.assertTrue(layout.same_shape(saved, tiles_now(fake, "23")), tiles_now(fake, "23"))

    def test_tiles_are_resized_to_their_saved_sizes(self):
        _, fake = self.run_restore()
        sizes = {fake._by(args["window"])["class"]: (args["x"], args["y"])
                 for m, args in fake.calls if m == "window.resize"}
        self.assertEqual(sizes, {"a": (1261, 688), "b": (624, 688), "c": (624, 688), "d": (1261, 1390)})

    def test_waiting_windows_are_not_rebuilt_on_the_screen_they_wait_on(self):
        # Saved on the TV's slot 3; the TV is unplugged, so they wait on the
        # portrait ROG, and the layout belongs to the TV.
        session = tv_layout_session(workspace="3")
        tv = "LG Electronics LG TV SSCR2 SERIAL3"
        for w in session["windows"]:
            w["monitor"] = tv
        source = empty(desk("tv-unplugged"))
        p = plan.build(session, source, entries())
        self.assertTrue(all(s.waiting for s in p.windows))
        report = restore.run(p, FakeDesk(source, OPENS), timeout=20)
        self.assertEqual(report.rebuilt, [])

    def test_other_layouts_get_swaps_not_a_rebuild(self):
        # Rebuilding drives dwindle's splits; under master or scrolling it
        # would fail or scramble, so the windows are only swapped.
        for tiled, shown in (("master", "master"), ("scrolling", "scrolling"), (None, "unknown")):
            with self.subTest(layout=tiled):
                source = empty(desk("tv-on-scene"))
                fake = FakeDesk(source, OPENS)
                fake.tiled_layout["23"] = tiled
                report, fake = self.run_restore(fake)
                self.assertEqual(report.count("placed"), 4)
                self.assertEqual(report.rebuilt, [])
                self.assertEqual(report.other_layout, {"23": shown})
                self.assertEqual(report.errors, [])
                self.assertFalse([c for c in fake.calls if c[0] == "layout"])
                self.assertFalse(any(c["workspace"]["name"] == restore.STAGING for c in fake.clients()))
                self.assertEqual(report.to_dict()["otherLayout"], {"23": shown})

    def test_a_layout_rule_on_another_workspace_does_not_stop_the_rebuild(self):
        source = empty(desk("tv-on-scene"))
        fake = FakeDesk(source, OPENS)
        fake.tiled_layout["5"] = "master"
        report, _ = self.run_restore(fake)
        self.assertEqual(report.rebuilt, ["23"])
        self.assertEqual(report.other_layout, {})

    def test_preselects_follow_the_tree(self):
        _, fake = self.run_restore()
        self.assertEqual([args["message"] for m, args in fake.calls if m == "layout"],
                         ["preselect r", "preselect d", "preselect r"])

    def test_each_screen_gets_its_view_back_and_nothing_stays_parked(self):
        source = empty(desk("tv-on-scene"))
        fake = FakeDesk(source, OPENS)
        note = {"address": "0xf00d", "class": "note", "initialClass": "note", "title": "note",
                "mapped": True, "hidden": False, "floating": True, "monitor": 2,
                "workspace": {"id": 1, "name": "1"}, "at": [2400, 900], "size": [300, 200], "pid": 1,
                "fullscreen": 0, "pinned": False, "grouped": [], "focusHistoryID": 0}
        fake._clients.append(note)
        fake.focused = "0xf00d"  # you were in a window on the TV
        _, fake = self.run_restore(fake)
        views = {m["name"]: m["activeWorkspace"]["name"] for m in fake.monitors()}
        self.assertEqual(views["DP-3"], "22")  # ws 23 was shown while it was rebuilt
        self.assertEqual(fake.focused, "0xf00d")
        self.assertFalse(any(c["workspace"]["name"] == restore.STAGING for c in fake.clients()))

    def test_saved_in_order_needs_no_rebuild(self):
        # Arriving a, d, b, c, the small dwindle builds the saved tree by itself...
        # ...only if each lands next to the right window; it doesn't here, so a
        # layout already matching is checked on an open, matching desk instead.
        report, fake = self.run_restore()
        again = restore.run(plan.build(tv_layout_session(), _source_of(fake), entries()), fake)
        self.assertEqual(again.rebuilt, [])
        self.assertEqual(again.count("in-place"), 4)

    def test_tiled_windows_the_session_does_not_know_block_a_rebuild(self):
        source = empty(desk("tv-on-scene"))
        fake = FakeDesk(source, OPENS)
        stranger = {"address": "0xbeef", "class": "stranger", "initialClass": "stranger", "title": "?",
                    "mapped": True, "hidden": False, "floating": False, "monitor": 2,
                    "workspace": {"id": 23, "name": "23"}, "at": [0, 0], "size": [10, 10], "pid": 1,
                    "fullscreen": 0, "pinned": False, "grouped": [], "focusHistoryID": 9}
        fake._clients.append(stranger)
        fake._workspaces.append({"id": 23, "name": "23", "monitor": "DP-3", "windows": 1})
        fake._insert(stranger)
        report, fake = self.run_restore(fake)
        self.assertEqual(report.rebuilt, [])
        self.assertEqual(fake._by("address:0xbeef")["workspace"]["name"], "23")

    def test_a_failure_midway_leaves_no_window_parked(self):
        source = empty(desk("tv-on-scene"))
        fake = FakeDesk(source, OPENS)

        def refuse(message):
            raise hypr.HyprError("layout refused")

        fake.layout = refuse
        report, fake = self.run_restore(fake)
        self.assertTrue(any("rebuild" in e for e in report.errors))
        self.assertFalse(any(c["workspace"]["name"] == restore.STAGING for c in fake.clients()))
        self.assertEqual({c["class"] for c in fake.clients() if c["workspace"]["name"] == "23"}, set(TILES))


def _source_of(fake):
    """The fake desk as a Source, to plan against it."""
    from snapback.capture import Source
    source = empty(desk("tv-on-scene"))
    source = Source(copy.deepcopy(source.snapshot), source.workspaces, source.instance)
    source.snapshot["clients"] = fake.clients()
    source.snapshot["workspaces"] = fake.workspaces()
    source.snapshot["monitors"] = fake.monitors()
    return source


if __name__ == "__main__":
    unittest.main()
