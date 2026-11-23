"""Recorded from the real desk while the TV was turned off, unplugged, and plugged back in.

Test windows are foot terminals with class snapback-test-*:
    t1, t2  tiled on ws 22 (main ROG, slot 2)
    f1      floating on ws 22 at +200,+150, 900x600
    f2      floating on ws 12 (portrait ROG, slot 2) at +300,+400, 800x1200
    f3      floating on ws 2 (TV, slot 2) at +100,+120, 700x450
    s1      on special:snapback-test
"""

import unittest
from pathlib import Path

from snapback.capture import Source, capture
from snapback.geometry import project

FIXTURES = Path(__file__).parent / "fixtures"
TV = "LG Electronics LG TV SSCR2 SERIAL3"
PORTRAIT = "Ancor Communications Inc ROG PG279Q #SERIAL1"
MAIN = "Ancor Communications Inc ROG PG279Q #SERIAL2"


def load(name):
    source = Source.from_dir(FIXTURES / name)
    return source, {w.app: w for w in capture(source)}


class TvOn(unittest.TestCase):
    def setUp(self):
        self.source, self.w = load("tv-on-scene")

    def test_floating_offsets_are_exact(self):
        expected = {
            "snapback-test-f1": (MAIN, 2, 200, 150, 900, 600),
            "snapback-test-f2": (PORTRAIT, 2, 300, 400, 800, 1200),
            "snapback-test-f3": (TV, 2, 100, 120, 700, 450),
        }
        for app, (owner, slot, dx, dy, w, h) in expected.items():
            win = self.w[app]
            self.assertTrue(win.floating, app)
            self.assertEqual((win.owner, win.slot), (owner, slot), app)
            p = win.placement
            self.assertEqual((p.dx, p.dy, p.w, p.h), (dx, dy, w, h), app)

    def test_special_workspace_is_kept_by_name(self):
        s1 = self.w["snapback-test-s1"]
        self.assertEqual(s1.workspace, "special:snapback-test")
        self.assertIsNone(s1.owner)

    def test_everything_projects_back_to_where_it_was(self):
        monitors = {m.identity: m for m in self.source.monitors()}
        clients = {c["address"]: c for c in self.source.snapshot["clients"]}
        for win in self.w.values():
            rect = project(win.placement, monitors[win.shown_on])
            c = clients[win.address]
            self.assertEqual((rect.x, rect.y, rect.w, rect.h), (*c["at"], *c["size"]), win.app)


class TvStandby(unittest.TestCase):
    """This TV keeps HDMI connected when switched off: Hyprland can't tell."""

    def test_tv_still_present_and_nothing_merged(self):
        source, w = load("tv-standby")
        self.assertIn(TV, {m.identity for m in source.monitors()})
        self.assertEqual(source.workspaces.merged, {})
        self.assertEqual((w["snapback-test-f3"].owner, w["snapback-test-f3"].slot), (TV, 2))


class TvUnplugged(unittest.TestCase):
    """Monitor disabled: Omarchy parked TV ws 1 and 2 on the portrait ROG as 13 and 14."""

    def setUp(self):
        self.source, self.w = load("tv-unplugged")

    def test_tv_gone(self):
        self.assertNotIn(TV, {m.identity for m in self.source.monitors()})

    def test_parked_windows_still_belong_to_the_tv(self):
        chromium, f3 = self.w["chromium"], self.w["snapback-test-f3"]
        self.assertEqual((chromium.workspace, chromium.owner, chromium.slot), ("13", TV, 1))
        self.assertEqual((f3.workspace, f3.owner, f3.slot), ("14", TV, 2))
        self.assertEqual(chromium.shown_on, PORTRAIT)

    def test_hyprland_keeps_floating_offset_on_the_new_monitor(self):
        p = self.w["snapback-test-f3"].placement
        self.assertEqual((p.dx, p.dy, p.w, p.h), (100, 120, 700, 450))

    def test_tv_slot_maps_back_to_its_own_block(self):
        self.assertEqual(self.source.workspaces.workspace_for(TV, 1), 1)
        self.assertEqual(self.source.workspaces.workspace_for(TV, 2), 2)


class TvReplugged(unittest.TestCase):
    """Monitor re-enabled. Omarchy 4.5.0 put ws 2 back, but ws 1 came back as ws 3,
    because Hyprland had already created an empty ws 1 on the returning TV.
    These tests pin that behavior so Snapback's fix-up can be written against it."""

    def setUp(self):
        self.source, self.w = load("tv-replugged")

    def test_merges_cleared(self):
        self.assertEqual(self.source.workspaces.merged, {})

    def test_ws2_returned_home_with_exact_geometry(self):
        f3 = self.w["snapback-test-f3"]
        self.assertEqual((f3.workspace, f3.owner, f3.slot, f3.shown_on), ("2", TV, 2, TV))
        p = f3.placement
        self.assertEqual((p.dx, p.dy), (100, 120))

    def test_ws1_came_back_in_the_wrong_slot(self):
        chromium = self.w["chromium"]
        self.assertEqual((chromium.workspace, chromium.owner, chromium.slot), ("3", TV, 3))


if __name__ == "__main__":
    unittest.main()
