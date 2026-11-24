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
    return DesktopEntry(entry_id, entry_id, "x", wm_class, False, terminal)


ENTRIES = {e.id: e for e in (
    app("chromium.desktop", "chromium"),
    app("t3code.desktop", "com.t3tools.T3Code"),
    app("foot.desktop", terminal=True),
)}
# All recordings come from one Hyprland session, so window ids carry over
# between them; their scrubbed titles don't.
RIG = "recording-instance"

# t1 stands in for a terminal opened in a folder; f1-f3 and s1 stay unresolved.
MAPPING = {"snapback-test-t1": "foot.desktop"}


def saved_scene():
    source = Source.from_dir(FIXTURES / "tv-on-scene")
    source.instance = RIG
    procs = lambda pid: ProcInfo(shell_cwd="/home/me/work")  # noqa: E731
    return build(source, Resolver(ENTRIES, MAPPING), procs, {},
                 datetime(2026, 11, 22, tzinfo=timezone.utc))


def desk(name, instance=RIG):
    source = Source.from_dir(FIXTURES / name)
    source.instance = instance
    return source


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

    def test_only_a_terminal_opens_in_a_folder(self):
        session = saved_scene()
        t1 = next(w for w in session["windows"] if w["app"] == "snapback-test-t1")
        t1["launch"]["entry"] = "t3code.desktop"  # a setup file edited by hand
        _, w = make(session, empty(desk("tv-on-scene")))
        self.assertEqual(w["snapback-test-t1"].action, "problem")
        self.assertIsNone(w["snapback-test-t1"].command)

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

        source = desk("tv-on-scene", instance="after-reboot")  # titles only
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


class WhichWindowIsWhich(unittest.TestCase):
    """The open window that fills a saved slot, in each mode."""

    def test_same_session_follows_the_window_through_title_changes(self):
        # T3 Code's title differs between the two recordings.
        _, w = make(saved_scene(), desk("tv-unplugged"))
        self.assertEqual(w["com.t3tools.T3Code"].matched_by, "window")

    def test_new_session_matches_titles_then_any_window_of_the_app(self):
        _, w = make(saved_scene(), desk("tv-unplugged", instance="after-reboot"))
        self.assertEqual(w["chromium"].matched_by, "title")  # same scrubbed title in both
        # T3 Code's title changed and its window ids mean nothing after a
        # reboot, but it is open: it is moved, not opened a second time.
        t3 = w["com.t3tools.T3Code"]
        self.assertEqual((t3.action, t3.matched_by), ("move", "app"))

    def test_unknown_session_never_counts_as_the_same_one(self):
        session = saved_scene()
        session["hyprland_instance"] = None
        _, w = make(session, desk("tv-unplugged", instance=None))
        self.assertEqual(w["com.t3tools.T3Code"].matched_by, "app")

    def test_an_open_window_of_the_app_is_used_before_launching(self):
        # The saved Chromium window was closed and another one is open, on
        # another tab: it is moved into the saved one's place.
        session = saved_scene()
        session["windows"] = [w for w in session["windows"] if w["app"] == "chromium"]
        source = desk("tv-on-scene")
        client = next(c for c in source.snapshot["clients"] if c["initialClass"] == "chromium")
        client["stableId"], client["title"] = "1800ffff", "Something else"
        result, w = make(session, source)
        self.assertEqual((w["chromium"].window, w["chromium"].matched_by), (client["address"], "app"))
        self.assertEqual(result.untouched, len(source.snapshot["clients"]) - 1)

    def test_the_window_already_in_place_is_the_one_used(self):
        session = saved_scene()
        session["windows"] = [w for w in session["windows"] if w["app"] == "chromium"]
        saved = session["windows"][0]
        saved["title"], saved["stable_id"] = "old tab", None
        source = desk("tv-on-scene")
        client = next(c for c in source.snapshot["clients"] if c["initialClass"] == "chromium")
        elsewhere = dict(copy.deepcopy(client), address="0xfeed", stableId="1",
                         workspace={"id": 5, "name": "5"}, focusHistoryID=0)
        client["focusHistoryID"] = 3  # used less recently than the one elsewhere
        source.snapshot["clients"].append(elsewhere)
        _, w = make(session, source)
        self.assertEqual((w["chromium"].window, w["chromium"].action), (client["address"], "in-place"))

    def test_one_open_window_fills_only_one_saved_one(self):
        session = saved_scene()
        a = next(w for w in session["windows"] if w["app"] == "chromium")
        session["windows"].append(dict(a, title="second", stable_id=None))
        result, _ = make(session, desk("tv-on-scene"))
        chromium = sorted(w.action for w in result.windows if w.app == "chromium")
        self.assertEqual(chromium, ["in-place", "launch"])

    def test_the_same_window_beats_a_title_match(self):
        session = saved_scene()
        a = next(w for w in session["windows"] if w["app"] == "chromium")
        b = dict(a, stable_id="1800ffff", title="Mail", slot=2)
        a["title"] = "Mail-old"
        session["windows"].append(b)
        source = desk("tv-on-scene")
        client = next(c for c in source.snapshot["clients"] if c["initialClass"] == "chromium")
        client["title"] = "Mail"  # the saved b's title, but it is a's window
        result = plan.build(session, source, ENTRIES)
        by_title = {x.title: x for x in result.windows if x.app == "chromium"}
        self.assertEqual((by_title["Mail-old"].window, by_title["Mail-old"].matched_by),
                         (client["address"], "window"))
        self.assertEqual(by_title["Mail"].action, "launch")


class TerminalApps(unittest.TestCase):
    def test_omarchy_tui_command(self):
        session = saved_scene()
        chromium = next(w for w in session["windows"] if w["app"] == "chromium")
        chromium.update(app="org.omarchy.btop", title="btop", stable_id=None,
                        launch={"how": "tui", "entry": "btop.desktop", "via": "omarchy-tui",
                                "app_id": "org.omarchy.btop"})
        btop = DesktopEntry("btop.desktop", "btop", "btop --utf-force %U", None, False, False, True)
        entries = dict(ENTRIES, **{"btop.desktop": btop})
        result = plan.build(session, empty(desk("tv-on-scene")), entries)
        step = next(w for w in result.windows if w.app == "org.omarchy.btop")
        self.assertEqual(step.command, ["uwsm-app", "--", "xdg-terminal-exec", "--app-id=org.omarchy.btop",
                                        "-e", "btop", "--utf-force"])

        chromium["launch"]["app_id"] = "x; rm -rf ~"
        result = plan.build(session, empty(desk("tv-on-scene")), entries)
        step = next(w for w in result.windows if w.app == "org.omarchy.btop")
        self.assertEqual((step.action, step.command), ("problem", None))


class Fullscreen(unittest.TestCase):
    def saved(self, app, mode):
        session = saved_scene()
        next(w for w in session["windows"] if w["app"] == app)["fullscreen"] = mode
        return session

    def test_mode_is_planned_with_a_note(self):
        _, w = make(self.saved("snapback-test-t1", 2), desk("tv-on-scene"))
        self.assertEqual((w["snapback-test-t1"].fullscreen, w["snapback-test-t1"].notes), (2, ["full screen"]))
        _, w = make(self.saved("snapback-test-t1", 1), desk("tv-on-scene"))
        self.assertEqual((w["snapback-test-t1"].fullscreen, w["snapback-test-t1"].notes), (1, ["full width"]))

    def test_saved_box_is_the_screens_so_it_is_not_its_tile_or_spot(self):
        _, w = make(self.saved("snapback-test-t1", 2), desk("tv-on-scene"))
        self.assertIsNone(w["snapback-test-t1"].tile_box)
        self.assertIsNone(w["snapback-test-t1"].tile)
        _, w = make(self.saved("snapback-test-f1", 2), desk("tv-on-scene"))
        self.assertIsNone(w["snapback-test-f1"].geometry)
        self.assertIsNotNone(w["snapback-test-t2"].tile_box)

    def test_not_full_screen_yet_is_not_in_place(self):
        _, w = make(self.saved("snapback-test-t1", 2), desk("tv-on-scene"))
        self.assertEqual(w["snapback-test-t1"].action, "move")

    def test_full_screen_now_but_not_saved_so_is_not_in_place(self):
        source = desk("tv-on-scene")
        next(c for c in source.snapshot["clients"] if c["initialClass"] == "snapback-test-t2")["fullscreen"] = 2
        _, w = make(saved_scene(), source)
        self.assertEqual(w["snapback-test-t2"].action, "move")

    def test_waiting_for_its_monitor_ignores_full_screen(self):
        _, w = make(self.saved("com.t3tools.T3Code", 2), desk("tv-unplugged"))
        t3 = w["com.t3tools.T3Code"]
        self.assertTrue(t3.waiting)
        self.assertIn("not made full screen while its monitor is away", t3.notes)

    def test_odd_values_mean_not_full_screen(self):
        for value in (None, 0, 3, -1, "x", "2", True, 1.5):
            self.assertEqual(plan.fullscreen_mode({"fullscreen": value}), 0)
        self.assertEqual(plan.fullscreen_mode({}), 0)


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


class SwitchingAndViews(unittest.TestCase):
    """What switching to a session closes, and what each screen shows."""

    def test_others_are_the_open_windows_the_session_does_not_name(self):
        session = saved_scene()
        session["windows"] = [w for w in session["windows"] if w["app"] == "chromium"]
        source = desk("tv-on-scene")
        result, w = make(session, source)
        addresses = {o["address"] for o in result.others}
        self.assertNotIn(w["chromium"].window, addresses)
        regular = [c for c in source.snapshot["clients"]
                   if c["workspace"]["id"] > 0 and c["address"] != w["chromium"].window]
        self.assertEqual(addresses, {c["address"] for c in regular})
        # A window on a special workspace (the scratchpad) is never closed.
        self.assertTrue(any(c["workspace"]["id"] < 0 for c in source.snapshot["clients"]))
        self.assertFalse(any(o["workspace"].startswith("special:") for o in result.others))
        t3 = next(o for o in result.others if o["app"] == "com.t3tools.T3Code")
        self.assertEqual(t3["name"], "t3code.desktop")  # the entry's name (ENTRIES names = ids)

    def test_apps_never_restored_stay_open(self):
        # A Zoom call: the setup can't bring it back, so a switch doesn't end it.
        session = dict(saved_scene(), windows=[])
        result, _ = make(session, desk("tv-on-scene"), never=lambda text: text == "com.t3tools.T3Code")
        self.assertNotIn("com.t3tools.T3Code", {o["app"] for o in result.others})
        self.assertIn("chromium", {o["app"] for o in result.others})
        # Nor an app the setup itself saved as skipped.
        session = saved_scene()
        session["windows"] = [dict(w, skip="never restore: zoom") if w["app"] == "chromium" else w
                              for w in session["windows"]]
        result, _ = make(session, desk("tv-on-scene"))
        self.assertNotIn("chromium", {o["app"] for o in result.others})

    def test_a_pinned_window_and_its_apps_other_windows_stay_open(self):
        # A call's picture-in-picture is pinned; the browser window holding the
        # call isn't, and nothing links them.
        session = dict(saved_scene(), windows=[])
        source = desk("tv-on-scene")
        pip = next(c for c in source.snapshot["clients"] if c["initialClass"] == "snapback-test-f2")
        pip["pinned"] = True
        call = copy.deepcopy(pip)
        call.update(address="0xcall", pinned=False, floating=False, workspace={"id": 1, "name": "1"})
        source.snapshot["clients"].append(call)
        result, _ = make(session, source)
        self.assertEqual({o["address"] for o in result.pinned}, {pip["address"], "0xcall"})
        closing = {o["address"] for o in result.others}
        self.assertFalse(closing & {pip["address"], "0xcall"})
        self.assertIn(next(c["address"] for c in source.snapshot["clients"]
                           if c["initialClass"] == "chromium"), closing)

    def test_without_a_pin_an_apps_windows_close_as_before(self):
        result, _ = make(dict(saved_scene(), windows=[]), desk("tv-on-scene"))
        self.assertEqual(result.pinned, [])

    def test_a_pinned_window_is_never_taken_for_a_saved_one(self):
        source = desk("tv-on-scene")
        for c in source.snapshot["clients"]:
            if c["initialClass"] == "snapback-test-f2":
                c["pinned"] = True
        result, w = make(saved_scene(), source)
        self.assertEqual(w["snapback-test-f2"].window, None)
        self.assertEqual([o["app"] for o in result.pinned], ["snapback-test-f2"])

    def test_nothing_to_close_when_everything_is_in_the_session(self):
        result, _ = make(saved_scene(), desk("tv-on-scene"))
        self.assertEqual(result.others, [])

    def test_empty_session_closes_every_regular_window(self):
        session = dict(saved_scene(), windows=[])
        source = desk("tv-on-scene")
        result, _ = make(session, source)
        self.assertEqual(len(result.others),
                         sum(1 for c in source.snapshot["clients"] if c["workspace"]["id"] > 0))

    def test_views_come_back_by_slot_on_each_screen(self):
        result, _ = make(saved_scene(), desk("tv-on-scene"))
        self.assertEqual(result.views, {"DP-2": "12", "DP-3": "22", "HDMI-A-1": "1"})

    def test_views_follow_omarchy_blocks_and_skip_unplugged_screens(self):
        result, _ = make(saved_scene(), desk("tv-unplugged"))
        self.assertEqual(result.views, {"DP-2": "12", "DP-3": "22"})  # the TV isn't there

    def test_sessions_without_views_show_what_they_show(self):
        session = saved_scene()
        for m in session["monitors"]:
            m.pop("view")
        result, _ = make(session, desk("tv-on-scene"))
        self.assertEqual(result.views, {})

    def test_app_name(self):
        entries = {"t3code.desktop": DesktopEntry("t3code.desktop", "T3 Code", "x", "com.t3tools.T3Code",
                                                  False, False),
                   "foot.desktop": DesktopEntry("foot.desktop", "Foot", "x", None, False, True)}
        self.assertEqual(plan.app_name("com.t3tools.T3Code", entries), "T3 Code")
        self.assertEqual(plan.app_name("foot", entries), "Foot")
        self.assertEqual(plan.app_name("weird", entries), "weird")


CHROMIUM = "0x55a4304d59d0"  # the open Chromium window in tv-on-scene
DASH = ["https://dashboard.lan/d/tv", "https://github.com/repos"]


def with_tabs(urls=DASH, profile="Default"):
    session = saved_scene()
    session["windows"] = [w for w in session["windows"] if w["app"] == "chromium"]
    session["windows"][0]["tabs"] = {"profile": profile, "urls": list(urls)}
    return session


class BrowserTabs(unittest.TestCase):
    def test_an_open_window_with_other_tabs_is_not_taken_for_it(self):
        result, w = make(with_tabs(), desk("tv-on-scene"), tabs={CHROMIUM: {"profile": "Default", "urls": []}})
        step = w["chromium"]
        self.assertEqual(step.action, "launch")
        self.assertEqual(step.command, ["uwsm-app", "--", "x", "--new-window", "--", *DASH])
        self.assertEqual(step.tabs, DASH)
        self.assertIn(CHROMIUM, {o["address"] for o in result.others})  # a switch closes it

    def test_the_window_holding_the_tabs_is_kept(self):
        tabs = {CHROMIUM: {"profile": "Default", "urls": ["https://github.com/x", "https://dashboard.lan/d/other"]}}
        result, w = make(with_tabs(), desk("tv-on-scene"), tabs=tabs)
        self.assertEqual((w["chromium"].action, w["chromium"].window), ("in-place", CHROMIUM))
        self.assertEqual(result.others and [o for o in result.others if o["address"] == CHROMIUM], [])

    def test_tabs_beat_the_title(self):
        source = desk("tv-on-scene", instance="another")
        copy_ = copy.deepcopy(next(c for c in source.snapshot["clients"] if c["address"] == CHROMIUM))
        copy_.update(address="0xdash", title="Grafana - Chromium", stableId="ffff", focusHistoryID=8)
        source.snapshot["clients"].append(copy_)
        tabs = {CHROMIUM: {"profile": "Default", "urls": ["https://news.example/"]},
                "0xdash": {"profile": "Default", "urls": DASH}}
        _, w = make(with_tabs(), source, tabs=tabs)
        self.assertEqual((w["chromium"].window, w["chromium"].matched_by), ("0xdash", "tabs"))

    def test_unknown_tabs_reuse_an_open_window_as_before(self):
        _, w = make(with_tabs(), desk("tv-on-scene", instance="another"))
        self.assertEqual((w["chromium"].window, w["chromium"].matched_by), (CHROMIUM, "title"))

    def test_a_closed_browser_that_restores_itself_is_started_plainly(self):
        asked = []
        cold = lambda app, profile: asked.append((app, profile)) or True  # noqa: E731
        _, w = make(with_tabs(profile="Profile 1"), empty(desk("tv-on-scene")), cold=cold)
        self.assertEqual(w["chromium"].command, ["uwsm-app", "--", "chromium.desktop"])
        self.assertIsNone(w["chromium"].tabs)
        self.assertEqual(asked, [("chromium", "Profile 1")])
        self.assertTrue(any("its own tabs" in n for n in w["chromium"].notes))

    def test_a_switch_starts_a_closed_browser_first_and_still_opens_the_tabs(self):
        cold = lambda app, profile: True  # noqa: E731
        result, w = make(with_tabs(), empty(desk("tv-on-scene")), cold=cold, switching=True)
        self.assertEqual(w["chromium"].command, ["uwsm-app", "--", "x", "--new-window", "--", *DASH])
        self.assertEqual(w["chromium"].tabs, DASH)
        self.assertEqual(result.warm, {"chromium": ["uwsm-app", "--", "chromium.desktop"]})
        self.assertTrue(any("started first" in n for n in w["chromium"].notes))

    def test_a_running_browser_needs_no_start(self):
        result, _ = make(with_tabs(), empty(desk("tv-on-scene")), switching=True)
        self.assertEqual(result.warm, {})

    def test_saved_urls_are_checked_again_before_opening(self):
        _, w = make(with_tabs(["--evil", "chrome://newtab/"]), empty(desk("tv-on-scene")))
        self.assertEqual(w["chromium"].command, ["uwsm-app", "--", "chromium.desktop"])

    def test_saved_tabs_only_go_to_a_browser(self):
        session = with_tabs()
        session["windows"][0]["launch"]["entry"] = "t3code.desktop"  # a setup file edited by hand
        _, w = make(session, empty(desk("tv-on-scene")))
        self.assertEqual(w["chromium"].command, ["uwsm-app", "--", "t3code.desktop"])

    def test_saving_keeps_tabs_only_for_windows_that_have_some(self):
        source = desk("tv-on-scene")
        procs = lambda pid: ProcInfo()  # noqa: E731
        tabs = lambda clients: {CHROMIUM: {"profile": "Default", "urls": DASH},  # noqa: E731
                                "0x55a430c6c450": {"profile": "Default", "urls": []}}
        saved = build(source, Resolver(ENTRIES, MAPPING), procs, {}, tabs=tabs)
        by_app = {w["app"]: w for w in saved["windows"]}
        self.assertEqual(by_app["chromium"]["tabs"], {"profile": "Default", "urls": DASH})
        self.assertIsNone(by_app["com.t3tools.T3Code"]["tabs"])
