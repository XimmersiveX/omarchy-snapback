"""Reading browser windows' tabs, and opening a window with them again."""

import os
import struct
import tempfile
import unittest
from pathlib import Path

from snapback import browsers


def command(cid, body):
    return struct.pack("<H", len(body) + 1) + bytes([cid]) + body


def string(text):
    raw = text.encode()
    return struct.pack("<i", len(raw)) + raw + b"\0" * (-len(raw) % 4)


def string16(text):
    raw = text.encode("utf-16-le")
    return struct.pack("<i", len(text)) + raw + b"\0" * (-len(raw) % 4)


def nav(tab, index, url, title=""):
    body = struct.pack("<iii", 0, tab, index) + string(url) + string16(title)
    return command(browsers.UPDATE_NAVIGATION, body)


def snss(*commands):
    return b"SNSS" + struct.pack("<i", 3) + b"".join(commands)


def window_file(windows, closed_tabs=(), closed_windows=(), kinds=None):
    """windows: {window id: [(tab id, url, title)]}, first tab selected."""
    out = []
    for window, tabs in windows.items():
        for index, (tab, url, title) in enumerate(tabs):
            out += [command(0, struct.pack("<ii", window, tab)), command(2, struct.pack("<ii", tab, index)),
                    nav(tab, 0, url, title), command(7, struct.pack("<ii", tab, 0))]
        out.append(command(8, struct.pack("<ii", window, 0)))
    for window, kind in (kinds or {}).items():
        out.append(command(9, struct.pack("<ii", window, kind)))
    out += [command(16, struct.pack("<i", t)) for t in closed_tabs]
    out += [command(17, struct.pack("<i", w)) for w in closed_windows]
    return snss(*out)


def client(address, title, stable, cls="chromium", pinned=False):
    return {"address": address, "class": cls, "initialClass": cls, "title": title,
            "stableId": format(stable, "x"), "mapped": True, "pinned": pinned}


class Parse(unittest.TestCase):
    def test_windows_tabs_and_selected_title(self):
        data = window_file({10: [(1, "https://a.example/x", "A"), (2, "https://b.example/", "B")],
                            20: [(3, "https://c.example/", "")]})
        windows = browsers.parse(data)
        self.assertEqual([(w.id, w.urls, w.title) for w in windows],
                         [(10, ["https://a.example/x", "https://b.example/"], "A"),
                          (20, ["https://c.example/"], "")])

    def test_later_navigation_and_selection_win(self):
        data = window_file({10: [(1, "https://old.example/", "Old")]})
        data += nav(1, 1, "https://new.example/", "New") + command(7, struct.pack("<ii", 1, 1))
        self.assertEqual(browsers.parse(data)[0].urls, ["https://new.example/"])
        self.assertEqual(browsers.parse(data)[0].title, "New")

    def test_closed_tabs_windows_and_app_windows_are_left_out(self):
        data = window_file({10: [(1, "https://a.example/", ""), (2, "https://b.example/", "")],
                            20: [(3, "https://c.example/", "")], 30: [(4, "https://d.example/", "")]},
                           closed_tabs=[2], closed_windows=[20], kinds={30: 2})
        self.assertEqual([(w.id, w.urls) for w in browsers.parse(data)], [(10, ["https://a.example/"])])

    def test_garbage_is_no_windows_not_a_crash(self):
        self.assertEqual(browsers.parse(b"not a session file"), [])
        good = window_file({10: [(1, "https://a.example/", "A")]})
        self.assertEqual(len(browsers.parse(good + b"\xff\xff\x06junk")), 1)  # torn tail
        broken = snss(command(6, b"\x01\x02"), command(0, b"\x01"))  # short bodies
        self.assertEqual(browsers.parse(broken), [])
        lying = snss(command(6, struct.pack("<iiii", 0, 1, 0, 1 << 30)))  # string longer than the body
        self.assertEqual(browsers.parse(lying), [])


class Urls(unittest.TestCase):
    def test_only_plain_web_and_file_urls_are_opened(self):
        for url in ("https://dashboard.lan/d/x?orgId=1", "http://10.0.0.5:3000/", "file:///home/me/a.html"):
            self.assertTrue(browsers.usable(url), url)
        for url in ("--renderer-cmd-prefix=evil", "-x", "chrome://newtab/", "javascript:alert(1)",
                    "https://a.example/ b", "https://a.example/\n--x", "", "data:text/html,hi", None):
            self.assertFalse(browsers.usable(url), url)

    def test_a_window_holds_saved_tabs_by_site(self):
        saved = ["https://dashboard.lan/d/a", "https://github.com/x"]
        self.assertTrue(browsers.holds(["https://github.com/y", "https://dashboard.lan/d/b", "https://z.example/"],
                                       saved))
        self.assertFalse(browsers.holds(["https://github.com/y"], saved))
        self.assertFalse(browsers.holds(["https://dashboard.lan/"], ["https://dashboard.lan/a", "https://dashboard.lan/b"]))

    def test_new_window_command(self):
        command = browsers.new_window(["chromium"], "Default", ["https://a.example/", "chrome://newtab/"])
        self.assertEqual(command, ["uwsm-app", "--", "chromium", "--new-window", "--", "https://a.example/"])
        command = browsers.new_window(["brave"], "Profile 2", ["https://a.example/"])
        self.assertIn("--profile-directory=Profile 2", command)
        self.assertEqual(command[-3:], ["--new-window", "--", "https://a.example/"])
        self.assertIsNone(browsers.new_window(["chromium"], "Default", ["chrome://newtab/"]))
        self.assertNotIn("--profile-directory=--x", browsers.new_window(["chromium"], "--x", ["https://a.example/"]))


class Pair(unittest.TestCase):
    W = [browsers.SessionWindow(10, "Default", ["https://a/"], "Grafana"),
         browsers.SessionWindow(20, "Default", ["https://b/"], ""),
         browsers.SessionWindow(30, "Default", ["https://c/"], "")]

    def test_the_order_windows_were_made_even_when_titles_disagree(self):
        # The session file keeps a page's old title when the page changes it itself.
        clients = [client("0x3", "Example - Chromium", 0x69), client("0x1", "Home - Grafana - Chromium", 0x61),
                   client("0x2", "New Tab - Chromium", 0x62)]
        paired = browsers.pair(clients, self.W, " - Chromium")
        self.assertEqual({a: w.id for a, w in paired.items()}, {"0x1": 10, "0x2": 20, "0x3": 30})

    def test_only_unique_titles_when_the_counts_differ(self):
        clients = [client("0x2", "x - Chromium", 0x62), client("0x1", "Grafana - Chromium", 0x61)]
        paired = browsers.pair(clients, self.W, " - Chromium")
        self.assertEqual({a: w.id for a, w in paired.items()}, {"0x1": 10})
        windows = [browsers.SessionWindow(10, "Default", ["https://a/"], "Mail"),
                   browsers.SessionWindow(20, "Default", ["https://b/"], "Mail")]
        clients = [client("0x1", "Mail - Chromium", 0x61)]
        self.assertEqual(browsers.pair(clients, windows, " - Chromium"), {})  # two windows have that title
        clients = [client("0x1", "Grafana - Chromium", 0x61), client("0x2", "Grafana - Chromium", 0x62)]
        self.assertEqual(browsers.pair(clients, self.W, " - Chromium"), {})  # and here two clients


class OnDisk(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.root = self.home / "chromium"
        sessions = self.root / "Default" / "Sessions"
        sessions.mkdir(parents=True)
        old = sessions / "Session_1"
        old.write_bytes(window_file({5: [(1, "https://stale.example/", "Stale")]}))
        os.utime(old, (1, 1))
        (sessions / "Session_2").write_bytes(window_file({
            10: [(1, "https://dashboard.lan/d/x", "Grafana"), (2, "chrome://settings/", "Settings")],
            20: [(3, "chrome://newtab/", "")]}))  # no title written yet: paired by order
        (self.root / "Default" / "Preferences").write_text('{"session": {"restore_on_startup": 1}}')

    def tearDown(self):
        self.tmp.cleanup()

    def test_open_tabs_from_the_newest_session_file(self):
        clients = [client("0x1", "Grafana - Chromium", 1), client("0x2", "New Tab - Chromium", 2),
                   client("0x9", "Picture in picture", 3, pinned=True), client("0x5", "Doc", 4, cls="foot")]
        tabs = browsers.open_tabs(clients, self.home)
        self.assertEqual(tabs, {"0x1": {"profile": "Default", "urls": ["https://dashboard.lan/d/x"]},
                                "0x2": {"profile": "Default", "urls": []}})

    def test_no_session_files_no_tabs(self):
        self.assertEqual(browsers.open_tabs([client("0x1", "x", 1, cls="google-chrome")], self.home), {})

    def test_running_means_a_live_lock_on_this_machine(self):
        browser = browsers.BROWSERS["chromium"]
        lock = self.root / "SingletonLock"
        self.assertFalse(browsers.running(browser, self.home))
        os.symlink(f"{os.uname().nodename}-{os.getpid()}", lock)
        self.assertTrue(browsers.running(browser, self.home))
        self.assertFalse(browsers.cold_restore("chromium", "Default", self.home))
        lock.unlink()
        os.symlink(f"other-host-{os.getpid()}", lock)
        self.assertFalse(browsers.running(browser, self.home))
        self.assertTrue(browsers.cold_restore("chromium", "Default", self.home))

    def test_cold_restore_only_when_set_to_continue(self):
        (self.root / "Default" / "Preferences").write_text('{"session": {"restore_on_startup": 5}}')
        self.assertFalse(browsers.cold_restore("chromium", "Default", self.home))
        self.assertFalse(browsers.cold_restore("chromium", "Profile 3", self.home))  # no such profile
        self.assertFalse(browsers.cold_restore("foot", "Default", self.home))


if __name__ == "__main__":
    unittest.main()
