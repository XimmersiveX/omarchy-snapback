import unittest

from snapback.geometry import Monitor, Placement, Rect, clamp, logical_size, monitors_from_hypr, project, relative


def mon(name, desc, x, y, width, height, scale=1.0, transform=0, reserved=(0, 0, 0, 0)):
    return {"name": name, "description": desc, "x": x, "y": y, "width": width, "height": height,
            "scale": scale, "transform": transform, "reserved": list(reserved)}


# The real three-screen desk, as Hyprland reports it (serials replaced).
DESK = [
    mon("DP-2", "ROG PG279Q #SERIAL1", 6400, 0, 2560, 1440, transform=1, reserved=(0, 26, 0, 0)),
    mon("DP-3", "ROG PG279Q #SERIAL2", 3840, 750, 2560, 1440, reserved=(0, 26, 0, 0)),
    mon("HDMI-A-1", "LG TV SERIAL3", 2240, 750, 3840, 2160, scale=2.4, reserved=(0, 26, 0, 0)),
]


def by_name(monitors):
    return {m.name: m for m in monitors}


class LogicalSize(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(logical_size(2560, 1440, 1, 0), (2560, 1440))

    def test_fractional_scale(self):
        self.assertEqual(logical_size(3840, 2160, 2.4, 0), (1600, 900))

    def test_portrait_swaps(self):
        self.assertEqual(logical_size(2560, 1440, 1, 1), (1440, 2560))
        self.assertEqual(logical_size(2560, 1440, 1, 3), (1440, 2560))
        self.assertEqual(logical_size(2560, 1440, 1, 2), (2560, 1440))


class Identities(unittest.TestCase):
    def test_desk_layout_is_contiguous(self):
        m = by_name(monitors_from_hypr(DESK))
        self.assertEqual(m["HDMI-A-1"].box, Rect(2240, 750, 1600, 900))
        self.assertEqual(m["HDMI-A-1"].box.right, m["DP-3"].box.x)   # TV meets the main ROG
        self.assertEqual(m["DP-3"].box.right, m["DP-2"].box.x)       # main ROG meets the portrait one
        self.assertEqual(m["DP-2"].box, Rect(6400, 0, 1440, 2560))
        self.assertEqual(m["DP-2"].usable, Rect(6400, 26, 1440, 2534))

    def test_twin_monitors_told_apart_by_serial(self):
        m = by_name(monitors_from_hypr(DESK))
        self.assertNotEqual(m["DP-2"].identity, m["DP-3"].identity)

    def test_identity_follows_the_monitor_not_the_port(self):
        swapped = [dict(DESK[0], name="DP-3"), dict(DESK[1], name="DP-2"), DESK[2]]
        m = by_name(monitors_from_hypr(swapped))
        self.assertEqual(m["DP-3"].identity, "ROG PG279Q #SERIAL1")
        self.assertEqual(m["DP-3"].transform, 1)

    def test_identical_descriptions_get_counted_like_omarchy(self):
        pair = [mon("DP-1", "Same Monitor", 0, 0, 1920, 1080), mon("DP-2", "Same Monitor", 1920, 0, 1920, 1080)]
        m = by_name(monitors_from_hypr(pair))
        self.assertEqual(m["DP-1"].identity, "Same Monitor")
        self.assertEqual(m["DP-2"].identity, "Same Monitor #2")

    def test_omarchy_state_wins_when_it_matches(self):
        pair = [mon("DP-1", "Same Monitor", 0, 0, 1920, 1080), mon("DP-2", "Same Monitor", 1920, 0, 1920, 1080)]
        known = {"DP-1": "Same Monitor #2", "DP-2": "Same Monitor"}
        m = by_name(monitors_from_hypr(pair, known))
        self.assertEqual(m["DP-1"].identity, "Same Monitor #2")
        self.assertEqual(m["DP-2"].identity, "Same Monitor")

    def test_omarchy_state_ignored_when_it_names_another_monitor(self):
        m = by_name(monitors_from_hypr(DESK, {"DP-2": "Something Else"}))
        self.assertEqual(m["DP-2"].identity, "ROG PG279Q #SERIAL1")

    def test_serial_is_not_mistaken_for_a_count(self):
        m = by_name(monitors_from_hypr(DESK, {"DP-2": "ROG PG279Q #SERIAL1"}))
        self.assertEqual(m["DP-2"].identity, "ROG PG279Q #SERIAL1")


class Projection(unittest.TestCase):
    def setUp(self):
        self.m = by_name(monitors_from_hypr(DESK))

    def assertInside(self, rect, area):
        self.assertGreaterEqual(rect.x, area.x)
        self.assertGreaterEqual(rect.y, area.y)
        self.assertLessEqual(rect.right, area.right)
        self.assertLessEqual(rect.bottom, area.bottom)

    def test_same_monitor_round_trips_exactly(self):
        for monitor in self.m.values():
            window = Rect(monitor.box.x + 100, monitor.box.y + 200, 640, 480)
            self.assertEqual(project(relative(window, monitor), monitor), window)

    def test_same_monitor_after_moving_it_in_the_layout(self):
        tv = self.m["HDMI-A-1"]
        saved = relative(Rect(2340, 950, 600, 400), tv)
        moved = Monitor(tv.name, tv.identity, Rect(0, 750, 1600, 900), Rect(0, 776, 1600, 874), 2.4, 0)
        self.assertEqual(project(saved, moved), Rect(100, 950, 600, 400))

    def test_tv_scale_changed_keeps_relative_center(self):
        # Saved at scale 2 (1920x1080 logical), restored at 2.4 (1600x900).
        old_tv = Monitor("HDMI-A-1", "LG TV SERIAL3", Rect(1920, 750, 1920, 1080),
                         Rect(1920, 776, 1920, 1054), 2.0, 0)
        saved = relative(Rect(1920 + 960 - 300, 750 + 540 - 200, 600, 400), old_tv)
        tv = self.m["HDMI-A-1"]
        out = project(saved, tv)
        self.assertEqual((out.w, out.h), (600, 400))
        self.assertEqual(out.center, tv.box.center)
        self.assertInside(out, tv.usable)

    def test_tv_window_falls_back_to_portrait_monitor(self):
        tv, portrait = self.m["HDMI-A-1"], self.m["DP-2"]
        saved = relative(Rect(2240, 776, 1600, 874), tv)  # TV-filling window
        out = project(saved, portrait)
        self.assertInside(out, portrait.usable)
        self.assertEqual(out.w, portrait.usable.w)
        self.assertAlmostEqual(out.w / out.h, 1600 / 874, places=2)

    def test_big_landscape_window_shrinks_onto_tv(self):
        main, tv = self.m["DP-3"], self.m["HDMI-A-1"]
        saved = relative(Rect(3840, 776, 2560, 1414), main)
        out = project(saved, tv)
        self.assertInside(out, tv.usable)

    def test_placement_survives_serialization(self):
        p = Placement(1, 2, 3, 4, 5, 6)
        self.assertEqual(Placement.from_dict(p.to_dict()), p)


class Clamp(unittest.TestCase):
    def test_offscreen_window_slides_back(self):
        area = Rect(0, 0, 1000, 800)
        self.assertEqual(clamp(Rect(-50, 900, 200, 100), area), Rect(0, 700, 200, 100))

    def test_oversized_window_is_cut_to_fit(self):
        area = Rect(0, 0, 1000, 800)
        self.assertEqual(clamp(Rect(10, 10, 2000, 2000), area), Rect(0, 0, 1000, 800))


if __name__ == "__main__":
    unittest.main()
