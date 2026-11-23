"""What `snapback status --json` tells the panel."""

import json
import stat
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from snapback import status
from snapback.apps import DesktopEntry
from snapback.session import SCHEMA

NOW = datetime(2026, 11, 23, 9, 0, tzinfo=timezone.utc)
ENTRIES = {"t3code.desktop": DesktopEntry("t3code.desktop", "T3 Code", "x", None, False, False,
                                          Path("t3code.desktop"))}


def window(app, entry=None, skip=None, problem=None):
    return {"app": app, "launch": {"entry": entry} if entry else None,
            "skip": skip, "problem": problem}


def session(saved_at, *windows):
    return {"snapback": SCHEMA, "saved_at": saved_at, "monitors": [{}, {}], "windows": list(windows)}


class Sessions(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def put(self, name, data):
        (self.dir / f"{name}.json").write_text(data if isinstance(data, str) else json.dumps(data))

    def test_own_sessions_first_then_named_newest_first(self):
        self.put("work", session("2026-11-20T10:00:00+00:00"))
        self.put("previous", session("2026-11-22T10:00:00+00:00"))
        self.put("games", session("2026-11-21T10:00:00+00:00"))
        self.put("last", session("2026-11-19T10:00:00+00:00"))
        names = [s["name"] for s in status.list_sessions(ENTRIES, self.dir)]
        self.assertEqual(names, ["last", "previous", "games", "work"])

    def test_counts_and_app_names(self):
        self.put("last", session(
            "2026-11-23T08:00:00+00:00",
            window("com.t3tools.T3Code", "t3code.desktop"),
            window("com.t3tools.T3Code", "t3code.desktop"),
            window("weird", problem="no app found"),
            window("steam", skip="never restore"),
        ))
        [s] = status.list_sessions(ENTRIES, self.dir)
        self.assertEqual(s["windows"], 3)        # the skipped one isn't counted
        self.assertEqual(s["reopen"], 2)
        self.assertEqual(s["attention"], 1)
        self.assertEqual(s["monitors"], 2)
        self.assertEqual(s["apps"], ["T3 Code", "weird"])  # entry name, else the class; once each

    def test_unreadable_session_is_listed_with_its_error(self):
        self.put("last", "{not json")
        self.put("old", {"snapback": SCHEMA + 1, "windows": []})
        found = {s["name"]: s for s in status.list_sessions(ENTRIES, self.dir)}
        self.assertIn("can't read", found["last"]["error"])
        self.assertIn("different Snapback version", found["old"]["error"])

    def test_ignores_temp_files_and_bad_names(self):
        self.put("last", session("2026-11-23T08:00:00+00:00"))
        (self.dir / ".last.abc.tmp").write_text("{}")
        (self.dir / "-x.json").write_text("{}")
        self.assertEqual([s["name"] for s in status.list_sessions(ENTRIES, self.dir)], ["last"])

    def test_missing_directory(self):
        self.assertEqual(status.list_sessions(ENTRIES, self.dir / "nope"), [])


class RestoreRecord(unittest.TestCase):
    REPORT = {
        "results": [
            {"app": "foot", "title": "a", "outcome": "placed", "detail": None},
            {"app": "foot", "title": "b", "outcome": "in-place", "detail": None},
            {"app": "steam", "title": "c", "outcome": "failed", "detail": "no window within 30 s"},
            {"app": "weird", "title": "d", "outcome": "unrestorable", "detail": "no app found"},
            {"app": "x", "title": "e", "outcome": "skipped", "detail": None},
        ],
        "extra": [], "reordered": [], "rebuilt": [], "errors": [],
    }

    def test_counts_and_problems(self):
        r = status.restore_record("last", "login", self.REPORT, now=NOW)
        self.assertEqual(r["at"], "2026-11-23T09:00:00+00:00")
        self.assertEqual((r["session"], r["trigger"]), ("last", "login"))
        self.assertEqual(r["counts"], {"placed": 1, "in-place": 1, "failed": 1,
                                       "skipped": 1, "unrestorable": 1})
        self.assertEqual([p["app"] for p in r["problems"]], ["steam", "weird"])
        self.assertEqual(r["problems"][0]["detail"], "no window within 30 s")
        self.assertFalse(r["ok"])
        self.assertIs(r["report"], self.REPORT)

    def test_clean_restore_is_ok(self):
        report = {"results": self.REPORT["results"][:2], "errors": []}
        self.assertTrue(status.restore_record("last", "manual", report)["ok"])

    def test_misplaced_windows_are_not_ok(self):
        report = {"results": self.REPORT["results"][:2], "errors": []}
        r = status.restore_record("last", "manual", report, check=[{"app": "foot"}])
        self.assertEqual(r["misplaced"], 1)
        self.assertFalse(r["ok"])

    def test_report_errors_are_not_ok(self):
        report = {"results": [], "errors": ["layout of ws 1 not rebuilt"]}
        r = status.restore_record("last", "manual", report)
        self.assertEqual(r["errors"], ["layout of ws 1 not rebuilt"])
        self.assertFalse(r["ok"])

    def test_restore_that_never_ran(self):
        r = status.restore_record("last", "login", error="no saved session at x")
        self.assertEqual(r["errors"], ["no saved session at x"])
        self.assertEqual(r["counts"]["placed"], 0)
        self.assertIsNone(r["report"])
        self.assertFalse(r["ok"])

    def test_written_privately_and_read_back(self):
        path = Path(tempfile.mkdtemp()) / "state" / "last-restore.json"
        record = status.restore_record("last", "login", self.REPORT, now=NOW)
        status.record_restore(record, path)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(status.last_restore(path), record)
        self.assertEqual(list(path.parent.iterdir()), [path])  # no temp file left

    def test_no_report_yet_or_a_broken_one(self):
        path = Path(tempfile.mkdtemp()) / "last-restore.json"
        self.assertIsNone(status.last_restore(path))
        path.write_text("[1, 2]")
        self.assertIsNone(status.last_restore(path))
        path.write_text("{")
        self.assertIsNone(status.last_restore(path))

    def test_an_unwritable_report_does_not_fail_the_restore(self):
        blocker = Path(tempfile.mkdtemp()) / "file"
        blocker.write_text("")
        status.record_restore({"x": 1}, blocker / "last-restore.json")  # parent is a file


class Hook(unittest.TestCase):
    def test_states(self):
        path = Path(tempfile.mkdtemp()) / "snapback"
        self.assertEqual(status.hook_state(path, "# ours"), "off")
        path.write_text("#!/bin/bash\n# ours\n")
        self.assertEqual(status.hook_state(path, "# ours"), "on")
        path.write_text("#!/bin/bash\necho mine\n")
        self.assertEqual(status.hook_state(path, "# ours"), "foreign")


if __name__ == "__main__":
    unittest.main()
