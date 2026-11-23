"""restore --dry-run: the plan for putting a saved session back.

The session is saved from the tv-on-scene recording and planned against the
recordings of the same desk (TV on, in standby, unplugged), and against a
desk with nothing open, as after a reboot.
"""

import copy
import unittest
from datetime import datetime, timezone
from pathlib import Path

from snapback import plan
from snapback.apps import DesktopEntry, ProcInfo, Resolver
from snapback.capture import Source
from snapback.geometry import Rect
from snapback.session import build

FIXTURES = Path(__file__).parent / "fixtures"
TV = "LG Electronics LG TV SSCR2 SERIAL3"
PORTRAIT = "Ancor Communications Inc ROG PG279Q #SERIAL1"
MAIN = "Ancor Communications Inc ROG PG279Q #SERIAL2"


def app(entry_id, wm_class=None, terminal=False):
    return DesktopEntry(entry_id, entry_id, "x", wm_class, False, terminal, Path(entry_id))


ENTRIES = {e.id: e for e in (
    app("chromium.desktop", "chromium"),
    app("t3code.desktop", "com.t3tools.T3Code"),
    app("foot.desktop", terminal=True),
)}
# t1 stands in for a terminal opened in a folder; f1-f3 and s1 stay unresolved.
MAPPING = {"snapback-test-t1": "foot.desktop"}


def saved_scene():
    source = Source.from_dir(FIXTURES / "tv-on-scene")
    procs = lambda pid: ProcInfo(shell_cwd="/home/me/work")  # noqa: E731
    return build(source, Resolver(ENTRIES, MAPPING), procs, {},
                 datetime(2026, 11, 22, tzinfo=timezone.utc))


def desk(name):
    return Source.from_dir(FIXTURES / name)


def empty(source):
    """The same desk with no windows and only each monitor's visible workspace."""
    source = copy.deepcopy(source)
    snap = source.snapshot
    snap["clients"] = []
    shown = {m["activeWorkspace"]["id"] for m in snap["monitors"]}
    snap["workspaces"] = [w for w in snap["workspaces"] if w["id"] in shown]
    return source


def make(session, source, **kwargs):
    kwargs.setdefault("default_terminal", "foot.desktop")
    kwargs.setdefault("is_dir", lambda path: True)
    result = plan.build(session, source, ENTRIES, **kwargs)
    return result, {w.app: w for w in result.windows}


class SameDesk(unittest.TestCase):
    def test_everything_is_already_in_place(self):
        result, _ = make(saved_scene(), desk("tv-on-scene"))
        self.assertEqual(result.count("in-place"), 9)
        self.assertEqual([w.action for w in result.workspaces], ["ok"] * len(result.workspaces))
        self.assertEqual(result.missing_monitors, [])
        self.assertEqual(result.untouched, 0)


class TvUnplugged(unittest.TestCase):
    def setUp(self):
        self.result, self.w = make(saved_scene(), desk("tv-unplugged"))

    def test_tv_windows_join_the_workspace_omarchy_merged_them_into(self):
        # Omarchy merged TV ws 1 into 13. Making ws 1 instead would leave two
        # copies, and Omarchy would renumber 13 when the TV returns.
        t3 = self.w["com.t3tools.T3Code"]
        self.assertEqual((t3.action, t3.now, t3.workspace), ("move", "22", "13"))
        self.assertEqual((t3.owner, t3.slot, t3.monitor, t3.waiting), (TV, 1, "DP-2", True))
        self.assertEqual(self.w["chromium"].action, "in-place")
        self.assertEqual(self.result.missing_monitors, [TV])

    def test_waiting_floating_window_keeps_its_spot(self):
        f3 = self.w["snapback-test-f3"]
        self.assertEqual((f3.action, f3.workspace), ("in-place", "14"))
        self.assertIsNone(f3.geometry)
        self.assertTrue(f3.notes)

    def test_other_windows_are_unaffected(self):
        self.assertEqual(self.w["snapback-test-f2"].action, "in-place")
        self.assertEqual(self.w["snapback-test-t1"].workspace, "22")


class TvStandby(unittest.TestCase):
    def test_tv_still_counts_as_plugged_in(self):
        result, w = make(saved_scene(), desk("tv-standby"))
        self.assertEqual(result.missing_monitors, [])
        self.assertEqual((w["com.t3tools.T3Code"].action, w["com.t3tools.T3Code"].workspace), ("move", "1"))


class AfterReboot(unittest.TestCase):
    """Nothing open: everything that can be launched is, onto its own screen."""

    def setUp(self):
        self.result, self.w = make(saved_scene(), empty(desk("tv-on-scene")))

    def test_apps_launch_through_their_desktop_entries(self):
        self.assertEqual(self.w["chromium"].command, ["uwsm-app", "--", "chromium.desktop"])
        self.assertEqual(self.w["com.t3tools.T3Code"].action, "launch")

    def test_terminal_opens_in_its_folder(self):
        t1 = self.w["snapback-test-t1"]
        self.assertEqual(t1.command, ["uwsm-app", "--", "xdg-terminal-exec", "--dir=/home/me/work"])
        self.assertEqual(t1.notes, [])

    def test_unresolved_windows_are_reported_not_guessed(self):
        f1 = self.w["snapback-test-f1"]
        self.assertEqual(f1.action, "problem")
        self.assertIn("snapback-test-f1", f1.reason)
        self.assertEqual(self.result.count("launch"), 3)

    def test_workspaces_are_made_on_their_owner_before_windows(self):
        steps = {w.workspace: w for w in self.result.workspaces}
        self.assertEqual((steps[1].monitor, steps[1].action), ("HDMI-A-1", "ok"))  # TV shows ws 1
        self.assertEqual((steps[2].monitor, steps[2].action), ("HDMI-A-1", "create"))
        self.assertEqual((steps[22].monitor, steps[22].action), ("DP-3", "ok"))
        self.assertEqual((steps[11].monitor, steps[11].action), ("DP-2", "create"))

    def test_floating_geometry_goes_back_on_its_own_monitor(self):
        f3 = self.w["snapback-test-f3"]
        self.assertEqual(f3.geometry, Rect(2240 + 100, 750 + 120, 700, 450))

    def test_tv_unplugged_at_login_waits_on_the_lowest_block(self):
        result, w = make(saved_scene(), empty(desk("tv-unplugged")))
        chromium = w["chromium"]
        # The recording's merged record (13 = TV ws 1) is stale once ws 13 is
        # gone, so the TV's own number, hosted on the portrait ROG (block 1),
        # the screen Omarchy would merge onto.
        self.assertEqual((chromium.workspace, chromium.monitor, chromium.waiting), ("1", "DP-2", True))
        steps = {s.workspace: s for s in result.workspaces}
        self.assertEqual((steps[1].action, steps[1].waiting), ("create", True))

    def test_waiting_workspace_that_exists_stays_where_it_is(self):
        source = empty(desk("tv-unplugged"))
        source.snapshot["workspaces"].append({"id": 1, "name": "1", "monitor": "DP-3", "windows": 0})
        result, w = make(saved_scene(), source)
        self.assertEqual((w["chromium"].workspace, w["chromium"].monitor), ("1", "DP-3"))
        step = next(s for s in result.workspaces if s.workspace == 1)
        self.assertEqual(step.action, "ok")


class Matching(unittest.TestCase):
    def test_same_class_windows_pair_by_title(self):
        session = saved_scene()
        a = next(w for w in session["windows"] if w["app"] == "chromium")
        b = copy.deepcopy(a)
        a["title"], b["title"] = "Docs", "Mail"
        b["slot"] = 2
        session["windows"].append(b)

        source = desk("tv-on-scene")
        client = next(c for c in source.snapshot["clients"] if c["initialClass"] == "chromium")
        client["title"] = "Mail"
        twin = dict(client, address="0xfeed", title="Docs", focusHistoryID=99)
        twin["workspace"] = {"id": 2, "name": "2"}
        source.snapshot["clients"].append(twin)

        result = plan.build(session, source, ENTRIES)
        by_title = {w.title: w for w in result.windows if w.app == "chromium"}
        self.assertEqual((by_title["Mail"].window, by_title["Mail"].action), (client["address"], "move"))
        self.assertEqual(by_title["Mail"].workspace, "2")
        self.assertEqual((by_title["Docs"].window, by_title["Docs"].action), ("0xfeed", "move"))

    def test_extra_saved_window_is_launched(self):
        session = saved_scene()
        extra = copy.deepcopy(next(w for w in session["windows"] if w["app"] == "chromium"))
        extra["title"] = "another"
        session["windows"].append(extra)
        result, _ = make(session, desk("tv-on-scene"))
        chromium = [w.action for w in result.windows if w.app == "chromium"]
        self.assertEqual(sorted(chromium), ["in-place", "launch"])

    def test_never_restore_matches_the_window_class(self):
        _, w = make(saved_scene(), desk("tv-on-scene"),
                    never=lambda text: text == "com.t3tools.T3Code")
        self.assertEqual(w["com.t3tools.T3Code"].action, "skip")

    def test_never_restore_now_skips_and_leaves_the_window_alone(self):
        result, w = make(saved_scene(), desk("tv-on-scene"),
                         never=lambda text: "chromium" in text)
        self.assertEqual(w["chromium"].action, "skip")
        self.assertEqual(result.untouched, 1)


class Changes(unittest.TestCase):
    def test_moved_floating_window_is_put_back(self):
        source = desk("tv-on-scene")
        client = next(c for c in source.snapshot["clients"] if c["initialClass"] == "snapback-test-f1")
        client["at"] = [client["at"][0] + 50, client["at"][1]]
        _, w = make(saved_scene(), source)
        self.assertEqual(w["snapback-test-f1"].action, "move")
        self.assertEqual(w["snapback-test-f1"].geometry, Rect(3840 + 200, 750 + 150, 900, 600))

    def test_workspace_on_the_wrong_monitor_is_moved(self):
        source = desk("tv-on-scene")
        for ws in source.snapshot["workspaces"]:
            if ws["id"] == 12:
                ws["monitor"] = "HDMI-A-1"
        result, _ = make(saved_scene(), source)
        step = next(s for s in result.workspaces if s.workspace == 12)
        self.assertEqual((step.action, step.now_on, step.monitor), ("move", "HDMI-A-1", "DP-2"))

    def test_uninstalled_app_and_missing_folder(self):
        session = saved_scene()
        source = empty(desk("tv-on-scene"))
        entries = {k: v for k, v in ENTRIES.items() if k != "t3code.desktop"}
        result = plan.build(session, source, entries, default_terminal="kitty.desktop",
                            is_dir=lambda path: False)
        w = {x.app: x for x in result.windows}
        self.assertEqual(w["com.t3tools.T3Code"].action, "problem")
        self.assertIn("no longer installed", w["com.t3tools.T3Code"].reason)
        t1 = w["snapback-test-t1"]
        self.assertEqual(t1.command, ["uwsm-app", "--", "xdg-terminal-exec"])
        self.assertEqual(len(t1.notes), 2)  # folder gone, different default terminal

    def test_session_saved_without_omarchy_keeps_numbers(self):
        session = saved_scene()
        for w in session["windows"]:
            if not w["workspace"].startswith("special:"):
                w["monitor"] = w["slot"] = None
        _, w = make(session, desk("tv-on-scene"))
        self.assertEqual((w["chromium"].workspace, w["chromium"].monitor), ("1", "HDMI-A-1"))
        self.assertEqual(w["chromium"].action, "in-place")


if __name__ == "__main__":
    unittest.main()
