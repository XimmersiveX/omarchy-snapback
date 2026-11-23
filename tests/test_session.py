import json
import stat
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from snapback import config
from snapback.apps import DesktopEntry, ProcInfo, Resolver
from snapback.capture import Source
from snapback.session import SCHEMA, SessionError, build, read, restorable, session_path, write

FIXTURES = Path(__file__).parent / "fixtures"
TV = "LG Electronics LG TV SSCR2 SERIAL3"


def app(entry_id, wm_class=None, terminal=False):
    return DesktopEntry(entry_id, entry_id, "x", wm_class, False, terminal, Path(entry_id))


ENTRIES = {e.id: e for e in (
    app("chromium.desktop", "chromium"),
    app("t3code.desktop", "com.t3tools.T3Code"),
    app("foot.desktop", terminal=True),
)}
NOW = datetime(2026, 11, 22, 21, 0, tzinfo=timezone.utc)


def scene(resolver=None, procs=None):
    source = Source.from_dir(FIXTURES / "tv-on-scene")
    return build(source, resolver or Resolver(ENTRIES), procs or (lambda pid: ProcInfo()),
                 {"hyprland": "v0.56.2"}, NOW)


class Build(unittest.TestCase):
    def setUp(self):
        self.session = scene()
        self.windows = {w["app"]: w for w in self.session["windows"]}

    def test_header(self):
        self.assertEqual(self.session["snapback"], SCHEMA)
        self.assertEqual(self.session["saved_at"], "2026-11-22T21:00:00+00:00")
        self.assertEqual(len(self.session["monitors"]), 3)
        tv = next(m for m in self.session["monitors"] if m["identity"] == TV)
        self.assertEqual((tv["connector"], tv["block"], tv["scale"]), ("HDMI-A-1", 0, 2.4))

    def test_windows_keep_monitor_and_slot(self):
        chromium = self.windows["chromium"]
        self.assertEqual((chromium["monitor"], chromium["slot"]), (TV, 1))
        self.assertEqual(chromium["launch"], {"how": "desktop", "entry": "chromium.desktop", "via": "wm-class"})

    def test_floating_window_placement(self):
        f3 = self.windows["snapback-test-f3"]
        self.assertTrue(f3["floating"])
        self.assertEqual((f3["placement"]["dx"], f3["placement"]["dy"]), (100, 120))

    def test_special_workspace_is_kept(self):
        s1 = self.windows["snapback-test-s1"]
        self.assertTrue(s1["workspace"].startswith("special:"))
        self.assertIsNone(s1["monitor"])

    def test_unresolved_windows_are_saved_with_the_problem(self):
        f1 = self.windows["snapback-test-f1"]
        self.assertIsNone(f1["launch"])
        self.assertIn("snapback-test-f1", f1["problem"])
        self.assertEqual(restorable(self.session), 2)

    def test_most_recently_focused_first(self):
        focus = [w["focus"] for w in self.session["windows"]]
        self.assertEqual(focus, sorted(focus))

    def test_never_restore_and_terminal_folder(self):
        resolver = Resolver(ENTRIES, {"snapback-test-t1": "foot"}, ["chromium"])
        session = scene(resolver, lambda pid: ProcInfo(shell_cwd="/home/me/work"))
        windows = {w["app"]: w for w in session["windows"]}
        self.assertIn("never restore", windows["chromium"]["skip"])
        self.assertEqual(windows["snapback-test-t1"]["launch"]["cwd"], "/home/me/work")

    def test_session_is_json(self):
        json.dumps(self.session)


class Files(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name) / "sessions"
        self.path = session_path("last", self.dir)

    def test_written_private_and_read_back(self):
        session = scene()
        write(session, self.path)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.dir.stat().st_mode), 0o700)
        self.assertEqual(read(self.path), session)
        self.assertEqual([p.name for p in self.dir.iterdir()], ["last.json"])  # no temp file left

    def test_empty_save_keeps_a_useful_one(self):
        write(scene(), self.path)
        empty = scene(Resolver({}))
        self.assertEqual(restorable(empty), 0)
        with self.assertRaises(SessionError):
            write(empty, self.path)
        self.assertEqual(restorable(read(self.path)), 2)
        write(empty, self.path, force=True)
        self.assertEqual(restorable(read(self.path)), 0)

    def test_empty_save_with_nothing_to_protect(self):
        write(scene(Resolver({})), self.path)
        self.assertTrue(self.path.exists())

    def test_names(self):
        for bad in ("", "../x", "a/b", ".hidden", "x.json", "a" * 65, "with space"):
            with self.assertRaises(SessionError, msg=bad):
                session_path(bad, self.dir)
        self.assertEqual(session_path("before-update_2", self.dir).name, "before-update_2.json")

    def test_other_format_is_refused(self):
        self.dir.mkdir()
        self.path.write_text(json.dumps({"snapback": 99}))
        with self.assertRaises(SessionError):
            read(self.path)
        with self.assertRaises(SessionError):
            read(self.dir / "missing.json")


class Config(unittest.TestCase):
    def load(self, text):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "config.toml"
        path.write_text(text)
        return config.load(path)

    def test_missing_file_gives_defaults(self):
        cfg = config.load(Path("/nonexistent/snapback.toml"))
        self.assertEqual((cfg.never_restore, cfg.apps), (["zoom"], {}))

    def test_values(self):
        cfg = self.load('never_restore = ["zoom", "1password"]\n[apps]\n"chrome-x-Default" = "x.desktop"\n')
        self.assertEqual(cfg.never_restore, ["zoom", "1password"])
        self.assertEqual(cfg.apps, {"chrome-x-Default": "x.desktop"})

    def test_empty_list_means_restore_everything(self):
        self.assertEqual(self.load("never_restore = []\n").never_restore, [])

    def test_bad_config(self):
        for text in ("never_restore = 'zoom'\n", "[apps]\nx = 1\n", "not toml ="):
            with self.assertRaises(config.ConfigError, msg=text):
                self.load(text)


if __name__ == "__main__":
    unittest.main()
