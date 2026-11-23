"""Setups on other screens than they were saved on: at work, on the laptop alone."""

import copy
import tempfile
import unittest
from pathlib import Path

from snapback import plan, screens
from snapback.capture import Source
from snapback.geometry import Rect, contains
from snapback.omarchy import WorkspaceState
from tests.test_plan import ENTRIES, desk, saved_scene

TV = "LG Electronics LG TV SSCR2 SERIAL3"
MIDDLE = "Ancor Communications Inc ROG PG279Q #SERIAL2"   # DP-3, the setup's main screen
RIGHT = "Ancor Communications Inc ROG PG279Q #SERIAL1"    # DP-2, portrait


def place(*monitors, blocks=None):
    """An empty desk with these monitors: (connector, identity, x, w, h)."""
    base = desk("tv-on-scene")
    snap = copy.deepcopy(base.snapshot)
    template = snap["monitors"][0]
    snap["monitors"] = []
    for i, (name, identity, x, w, h) in enumerate(monitors):
        m = copy.deepcopy(template)
        m.update(id=i, name=name, description=identity, x=x, y=0, width=w, height=h, scale=1,
                 transform=0, focused=i == 0, activeWorkspace={"id": 0, "name": ""})
        snap["monitors"].append(m)
    snap["clients"], snap["workspaces"] = [], []
    known = {TV: 0, RIGHT: 1, MIDDLE: 2}
    for i, (name, identity, *_rest) in enumerate(monitors):
        known.setdefault(identity, 3 + i)
    known.update(blocks or {})
    text = "\n".join(f"{b}\t{name if any(n == name for n, *_x in monitors) else ''}\t{identity}"
                     for identity, b in known.items()
                     for name in [next((n for n, ident, *_x in monitors if ident == identity), "")])
    return Source(snap, WorkspaceState.parse(text, ""), "elsewhere")


def build(source, session=None, marks=frozenset()):
    result = plan.build(session or saved_scene(), source, ENTRIES, default_terminal="foot.desktop",
                        is_dir=lambda path: True, marks=marks)
    return result, {w.app: w for w in result.windows}


WORK_A = ("DP-1", "Dell U2720Q A", 0, 2560, 1440)       # the larger: main
WORK_B = ("DP-2", "Dell P2419H B", 2560, 1920, 1080)    # to its right


class AtWork(unittest.TestCase):
    """Home: TV left, ROG middle (main), portrait ROG right. Work: two Dells."""

    def test_main_goes_to_main_and_the_right_screen_to_the_right(self):
        source = place(WORK_A, WORK_B)
        a, b = 3 * 10, 4 * 10   # their blocks
        result, w = build(source)
        self.assertEqual(w["snapback-test-t1"].workspace, str(a + 2))        # middle slot 2 -> A slot 2
        self.assertEqual(w["snapback-test-t1"].monitor, "DP-1")
        self.assertEqual(w["chrome-__home_user_.local_share_omarchy-hotkeys_hotkeys.html-Default"].workspace,
                         str(b + 1))                                         # right slot 1 -> B slot 1
        self.assertEqual(w["snapback-test-f2"].workspace, str(b + 2))
        # The TV has no screen on its side: it folds into A after A's own (slot 2).
        self.assertEqual(w["com.t3tools.T3Code"].workspace, str(a + 3))
        self.assertEqual(w["chromium"].workspace, str(a + 3))
        self.assertEqual(w["snapback-test-f3"].workspace, str(a + 4))
        self.assertTrue(all(not s.waiting for s in result.windows if s.action != "skip"))
        self.assertEqual(result.missing_monitors, [])
        how = {m["was"]: m["how"] for m in result.screens}
        self.assertEqual(how, {"DP-3": "main", "DP-2": "side", "HDMI-A-1": "folded"})

    def test_the_other_way_around(self):
        source = place(WORK_A, ("DP-2", "Dell P2419H B", -1920, 1920, 1080))
        _, w = build(source)
        self.assertEqual(w["com.t3tools.T3Code"].workspace, "41")   # TV (left) -> B (left)
        self.assertEqual(w["snapback-test-f3"].workspace, "42")
        self.assertEqual(w["chrome-__home_user_.local_share_omarchy-hotkeys_hotkeys.html-Default"].workspace,
                         "33")                                      # right folds into A after slot 2
        self.assertEqual(w["snapback-test-f2"].workspace, "34")

    def test_the_marked_main_screen_wins_over_the_largest(self):
        source = place(WORK_A, WORK_B)
        _, w = build(source, marks={"Dell P2419H B"})
        self.assertEqual(w["snapback-test-t1"].monitor, "DP-2")     # the setup's main -> B
        # A is left of B; the TV is the setup's left screen.
        self.assertEqual(w["com.t3tools.T3Code"].monitor, "DP-1")

    def test_floating_windows_land_inside_their_new_screen(self):
        source = place(WORK_A, WORK_B)
        result, _ = build(source)
        boxes = {m.name: m.box for m in source.monitors()}
        for step in result.windows:
            if step.geometry and step.monitor:
                g = step.geometry
                self.assertTrue(contains(boxes[step.monitor], g.x + g.w / 2, g.y + g.h / 2), step.app)

    def test_screens_show_the_saved_views_of_the_screens_they_stand_in_for(self):
        result, _ = build(place(WORK_A, WORK_B))
        self.assertEqual(result.views, {"DP-1": "32", "DP-2": "42"})  # middle and right showed slot 2


    def test_the_nearest_screen_on_a_side_is_used(self):
        source = place(WORK_A, WORK_B, ("DP-3", "Dell far C", 4480, 1920, 1080))
        _, w = build(source)
        self.assertEqual(w["snapback-test-f2"].monitor, "DP-2")  # B, not the farther C

    def test_main_screen_taken_by_one_of_the_setups_own(self):
        # The setup's right screen is here and is the main screen; the
        # setup's main screen folds into it rather than taking a side screen.
        source = place(("DP-1", RIGHT, 0, 2560, 1440), WORK_B)
        result, w = build(source)
        self.assertEqual(w["snapback-test-t1"].monitor, "DP-1")
        self.assertEqual({m["was"]: m["how"] for m in result.screens}["DP-3"], "folded")

class LaptopAlone(unittest.TestCase):
    LAPTOP = ("eDP-1", "BOE 0x0BCA", 0, 1920, 1200)

    def test_everything_folds_onto_the_laptop_after_the_main_screens_own(self):
        result, w = build(place(self.LAPTOP))
        base = 30
        self.assertEqual(w["snapback-test-t1"].workspace, str(base + 2))     # main keeps slot 2
        # Then left to right: the TV's slots 1 and 2, the right screen's 1 and 2.
        self.assertEqual(w["com.t3tools.T3Code"].workspace, str(base + 3))
        self.assertEqual(w["snapback-test-f3"].workspace, str(base + 4))
        self.assertEqual(w["chrome-__home_user_.local_share_omarchy-hotkeys_hotkeys.html-Default"].workspace,
                         str(base + 5))
        self.assertEqual(w["snapback-test-f2"].workspace, str(base + 6))
        self.assertEqual({s.monitor for s in result.windows if s.monitor}, {"eDP-1"})

    def test_special_workspaces_stay_special(self):
        _, w = build(place(self.LAPTOP))
        self.assertEqual(w["snapback-test-s1"].workspace, "special:snapback-test")

    def test_no_room_left_means_waiting(self):
        session = saved_scene()
        extra = next(x for x in session["windows"] if x["app"] == "chromium")
        for slot in range(3, 11):  # the TV now uses slots 1-10
            session["windows"].append(dict(extra, slot=slot, workspace=str(slot), title=f"t{slot}"))
        result, _ = build(place(self.LAPTOP), session)
        tv = [s for s in result.windows if s.app == "chromium"]
        self.assertTrue(any(s.waiting for s in tv))      # 10 TV slots don't fit after slot 2
        self.assertTrue(any(not s.waiting for s in tv))
        mapping = screens.map_screens(session, place(self.LAPTOP).monitors(), set())
        self.assertEqual(max(mapping.folded.values()), 10)


class AtHome(unittest.TestCase):
    def test_with_the_main_screen_there_a_missing_screen_waits_as_before(self):
        result, w = build(desk("tv-unplugged"))
        self.assertTrue(w["com.t3tools.T3Code"].waiting)
        self.assertEqual({m["how"] for m in result.screens}, {"same", "waiting"})

    def test_every_screen_there_maps_to_itself(self):
        result, _ = build(desk("tv-on-scene"))
        self.assertEqual({m["how"] for m in result.screens}, {"same"})


class Main(unittest.TestCase):
    def test_pick_main(self):
        big, small = Rect(0, 0, 2560, 1440), Rect(2560, 0, 1920, 1080)
        self.assertEqual(screens.pick_main([("a", small), ("b", big)], set()), "b")
        self.assertEqual(screens.pick_main([("a", small), ("b", big)], {"a"}), "a")
        self.assertEqual(screens.pick_main([("r", Rect(9, 0, 10, 10)), ("l", Rect(0, 0, 10, 10))], set()), "l")
        self.assertIsNone(screens.pick_main([], set()))

    def test_marks_are_one_per_place(self):
        path = Path(tempfile.mkdtemp()) / "screens.json"
        screens.mark_main("home-middle", ["home-tv", "home-middle", "home-right"], path)
        screens.mark_main("work-a", ["work-a", "work-b"], path)
        self.assertEqual(screens.load_marks(path), {"home-middle", "work-a"})
        screens.mark_main("work-b", ["work-a", "work-b"], path)
        self.assertEqual(screens.load_marks(path), {"home-middle", "work-b"})
        path.write_text("{broken")
        self.assertEqual(screens.load_marks(path), set())

    def test_a_setup_remembers_its_main_screen(self):
        session = saved_scene()
        self.assertEqual(screens.saved_main(session, set()), MIDDLE)   # largest, then leftmost
        for m in session["monitors"]:
            m["main"] = m["identity"] == RIGHT
        self.assertEqual(screens.saved_main(session, set()), RIGHT)     # as recorded
        for m in session["monitors"]:
            m.pop("main")
        self.assertEqual(screens.saved_main(session, {TV}), TV)         # older setups: today's mark


if __name__ == "__main__":
    unittest.main()
