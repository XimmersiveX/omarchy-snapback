"""The autosave watcher, the login hook, and the login summary."""

import shlex
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path

from snapback import restore, session, setup, watch
from snapback.__main__ import restore_summary
from snapback.plan import WindowStep


class Script:
    """A fake event socket and clock: events arrive at set times."""

    def __init__(self, events, end):
        self.events = sorted(events)    # (time, line)
        self.end = end                  # Hyprland goes away at this time
        self.now = 0.0
        self.saves = []
        self.checks = 0

    def clock(self):
        return self.now

    def next_event(self, timeout):
        deadline = self.now + timeout
        if self.events and self.events[0][0] <= deadline:
            self.now = max(self.now, self.events[0][0])
            return self.events.pop(0)[1]
        if self.end <= deadline:
            self.now = self.end
            return watch.EOF_EVENT
        self.now = deadline
        return None

    def run(self, may_save=lambda: None, save=None):
        def default_save():
            self.saves.append(self.now)
            return True

        def checked():
            self.checks += 1
            return may_save()

        watch.Watcher(save or default_save, checked, clock=self.clock).run(self.next_event)
        return self.saves


class Saving(unittest.TestCase):
    def test_saves_at_start_then_once_a_minute(self):
        saves = Script([], end=130).run()
        self.assertEqual(saves, [0, 60, 120])

    def test_waits_for_things_to_settle(self):
        saves = Script([(30, "openwindow>>a,1,foot,t"), (31, "movewindowv2>>a,2,2")], end=50).run()
        self.assertEqual(saves, [0, 34])  # 3 s after the last change

    def test_busy_titles_still_save_within_max_wait(self):
        churn = [(20 + i, "windowtitlev2>>a,title") for i in range(40)]
        saves = Script(churn, end=59).run()
        self.assertEqual(saves[:2], [0, 35])  # not starved until the churn stops at 59

    def test_never_more_often_than_min_interval(self):
        events = [(t, "openwindow>>x") for t in (1, 5, 9, 13)]
        saves = Script(events, end=30).run()
        self.assertTrue(all(b - a >= 10 for a, b in zip(saves, saves[1:])), saves)

    def test_focus_events_never_cause_a_save_or_block_the_minute_save(self):
        focus = [(t / 2, "activewindowv2>>a") for t in range(1, 260)]
        saves = Script(focus, end=129).run()
        self.assertEqual(saves, [0, 60, 120])

    def test_a_burst_of_closing_windows_holds_saves(self):
        closes = [(40, "closewindow>>a"), (40.5, "closewindow>>b"), (41, "closewindow>>c")]
        saves = Script(closes, end=60).run()
        self.assertEqual(saves, [0])  # 41 + 20 s calm is after the end

    def test_one_closed_window_is_saved(self):
        saves = Script([(40, "closewindow>>a")], end=50).run()
        self.assertEqual(saves, [0, 43])

    def test_no_saving_while_told_not_to_and_no_spinning(self):
        script = Script([(5, "openwindow>>a")], end=100)
        saves = script.run(may_save=lambda: "Omarchy is logging out")
        self.assertEqual(saves, [])
        self.assertLess(script.checks, 40)  # backs off instead of asking in a loop

    def test_a_refused_save_does_not_stop_the_watcher(self):
        script = Script([], end=70)

        def save():
            script.saves.append(script.now)
            raise session.SessionError("nothing to reopen")

        script.run(save=save)
        self.assertEqual(script.saves, [0, 60])


class Shutdown(unittest.TestCase):
    def proc(self, *cmdlines):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for pid, args in enumerate(cmdlines, start=100):
            (root / str(pid)).mkdir()
            (root / str(pid) / "cmdline").write_bytes(b"\0".join(a.encode() for a in args) + b"\0")
        (root / "self").mkdir()
        return root

    def test_omarchy_exit_is_seen(self):
        root = self.proc(["/bin/bash", "/usr/share/omarchy/bin/omarchy-system", "reboot", "--execute"])
        self.assertIn("Omarchy", watch.shutdown_in_progress(root))
        root = self.proc(["timeout", "2s", "omarchy-hyprland-window-close-all", "--wait"])
        self.assertIn("closing every window", watch.shutdown_in_progress(root))
        root = self.proc(["/usr/bin/python3", "/usr/bin/uwsm", "stop"])
        self.assertIsNotNone(watch.shutdown_in_progress(root))

    def test_ordinary_processes_are_not_an_exit(self):
        root = self.proc(["/usr/share/omarchy/bin/omarchy-system"], ["foot"], ["uwsm", "app", "--", "foot"])
        self.assertIsNone(watch.shutdown_in_progress(root))


class Locks(unittest.TestCase):
    def test_restore_running_follows_the_restore_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lock"
            self.assertFalse(watch.restore_running(path))
            with restore.Lock(path):
                self.assertTrue(watch.restore_running(path))
            self.assertFalse(watch.restore_running(path))

    def test_one_watcher_at_a_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "watch.lock"
            with watch.WatchLock(path):
                with self.assertRaises(session.SessionError):
                    with watch.WatchLock(path):
                        pass

    def test_comparable_ignores_the_time(self):
        a = {"snapback": 1, "saved_at": "1", "windows": [1]}
        self.assertEqual(watch.comparable(a), watch.comparable(dict(a, saved_at="2")))
        self.assertNotEqual(watch.comparable(a), watch.comparable(dict(a, windows=[2])))


class EventSocket(unittest.TestCase):
    def test_lines_split_across_reads_and_eof(self):
        ours, theirs = socket.socketpair()
        self.addCleanup(ours.close)
        next_event = watch.events_from(ours)
        theirs.sendall(b"openwindow>>a,1,foot,t\nclosewin")
        self.assertEqual(next_event(1), "openwindow>>a,1,foot,t")
        self.assertIsNone(next_event(0.01))  # half a line: nothing yet
        theirs.sendall(b"dow>>a\nmovewindowv2>>a,2,2\n")
        self.assertEqual(next_event(1), "closewindow>>a")
        self.assertEqual(next_event(1), "movewindowv2>>a,2,2")
        self.assertIsNone(next_event(0.01))
        theirs.close()
        self.assertIs(next_event(1), watch.EOF_EVENT)


class Hook(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "post-boot.d" / "snapback"

    def test_install_and_remove(self):
        setup.install(self.path, Path("/home/me/my code"))
        text = self.path.read_text()
        self.assertIn("systemd-run --user --unit=snapback", text)
        self.assertIn("'--setenv=PYTHONPATH=/home/me/my code'", text)
        self.assertTrue(text.rstrip().endswith("python3 -m snapback login --verbose"))
        setup.install(self.path)  # again is fine
        self.assertTrue(setup.remove(self.path))
        self.assertFalse(self.path.exists())
        self.assertFalse(setup.remove(self.path))

    def run_hook(self, root):
        """Run the hook as Omarchy does (bash <file>), with a stand-in systemd-run."""
        bin_dir = self.path.parent.parent / "bin"
        bin_dir.mkdir(exist_ok=True)
        started = bin_dir / "started"
        fake = bin_dir / "systemd-run"
        fake.write_text(f"#!/bin/sh\necho \"$@\" > {shlex.quote(str(started))}\n")
        fake.chmod(0o755)
        setup.install(self.path, root)
        env = {"PATH": f"{bin_dir}:/usr/bin:/bin"}
        result = subprocess.run(["bash", str(self.path)], env=env, capture_output=True, text=True)
        return result, started

    def test_hook_starts_snapback_while_it_is_installed(self):
        root = self.path.parent.parent / "plugin dir"
        (root / "snapback").mkdir(parents=True)
        result, started = self.run_hook(root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("snapback login", started.read_text())
        self.assertTrue(self.path.exists())

    def test_hook_removes_itself_once_snapback_is_gone(self):
        result, started = self.run_hook(self.path.parent.parent / "removed plugin")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(started.exists())
        self.assertFalse(self.path.exists())

    def test_never_touches_a_hook_it_did_not_write(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("#!/bin/bash\necho mine\n")
        with self.assertRaises(FileExistsError):
            setup.install(self.path)
        with self.assertRaises(FileExistsError):
            setup.remove(self.path)
        self.assertIn("mine", self.path.read_text())


class LoginSummary(unittest.TestCase):
    def report(self, *outcomes):
        report = restore.Report()
        for i, outcome in enumerate(outcomes):
            report.results.append(restore.Result(WindowStep(f"app{i}", "", "launch"), outcome))
        return report

    def test_all_back(self):
        self.assertEqual(restore_summary(self.report("placed", "in-place")),
                         ("Snapback restored 2 windows", "Everything is back where it was."))

    def test_names_what_failed(self):
        title, body = restore_summary(self.report("placed", "failed", "unrestorable"))
        self.assertEqual(title, "Snapback restored 1 window")
        self.assertEqual(body, "2 couldn't be restored: app1, app2")

    def test_says_when_windows_are_still_out_of_place(self):
        title, body = restore_summary(self.report("placed"), misplaced=2)
        self.assertEqual(body, "2 still out of place; restore again to finish.")
        _, body = restore_summary(self.report("placed", "failed"), misplaced=2)
        self.assertIn("couldn't be restored", body)  # failures say more than misplacement


if __name__ == "__main__":
    unittest.main()
