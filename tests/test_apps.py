import os
import tempfile
import unittest
from pathlib import Path

from snapback.apps import ProcInfo, Resolver, ids_from_unit, load_entries, parse_desktop_entry, read_proc


def entry(name, exec_, wm_class=None, **extra):
    lines = ["[Desktop Entry]", "Type=Application", f"Name={name}", f"Exec={exec_}"]
    if wm_class:
        lines.append(f"StartupWMClass={wm_class}")
    lines += [f"{k}={v}" for k, v in extra.items()]
    return "\n".join(lines) + "\n"


class Apps(unittest.TestCase):
    """A user dir and a system dir laid out like this desk's."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.user, self.system = root / "user", root / "system"
        files = {
            self.system / "applications/chromium.desktop":
                entry("Chromium", "chromium %U", "chromium")
                + "[Desktop Action new-window]\nName=New Window\nExec=chromium --new-window\n",
            self.system / "applications/foot.desktop":
                entry("Foot", "foot", Categories="System;TerminalEmulator;"),
            self.system / "applications/org.gnome.Nautilus.desktop": entry("Files", "nautilus"),
            self.system / "applications/vendor/tool.desktop": entry("Tool", "tool"),
            self.system / "applications/removed.desktop": entry("Removed", "removed", "removed"),
            self.user / "applications/removed.desktop": "[Desktop Entry]\nType=Application\nHidden=true\n",
            self.user / "applications/t3code.desktop":
                entry("T3 Code", "~/Applications/T3-Code.AppImage %U", "com.t3tools.T3Code"),
            self.user / "applications/com.t3tools.T3Code.desktop":
                entry("T3 Code (Beta)", "~/Applications/T3-Code.AppImage %U", "com.t3tools.T3Code", NoDisplay="true"),
            self.user / "applications/YouTube.desktop":
                entry("YouTube", "omarchy-launch-webapp https://youtube.com/", "chrome-youtube.com__-Default"),
            self.user / "applications/Zoom.desktop": entry("Zoom", "omarchy-webapp-handler-zoom %u"),
            self.user / "applications/omarchy-hotkeys.desktop": entry("Hotkeys", "~/.local/bin/omarchy-hotkeys"),
        }
        for path, text in files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        self.entries = load_entries([self.user, self.system])
        self.resolver = Resolver(self.entries, never_restore=["zoom"])

    def tearDown(self):
        self.tmp.cleanup()

    def launch(self, app_class, proc=None, resolver=None):
        result = (resolver or self.resolver).resolve(app_class, proc)
        self.assertIsNone(result.problem, result.problem)
        self.assertIsNone(result.skip, result.skip)
        return result.launch

    def test_entries_load_with_ids_and_masking(self):
        self.assertEqual(self.entries["chromium.desktop"].exec, "chromium %U")  # not the action's Exec
        self.assertIn("vendor-tool.desktop", self.entries)
        self.assertNotIn("removed.desktop", self.entries)  # the user's Hidden copy hides it

    def test_wm_class(self):
        launch = self.launch("chromium")
        self.assertEqual((launch.how, launch.entry, launch.via), ("desktop", "chromium.desktop", "wm-class"))

    def test_web_app_wm_class(self):
        self.assertEqual(self.launch("chrome-youtube.com__-Default").entry, "YouTube.desktop")

    def test_listed_entry_beats_hidden_one_for_same_class(self):
        # t3code.desktop claims the class; com.t3tools.T3Code.desktop is NoDisplay.
        self.assertEqual(self.launch("com.t3tools.T3Code").entry, "t3code.desktop")

    def test_entry_named_after_class(self):
        launch = self.launch("org.gnome.nautilus")
        self.assertEqual((launch.entry, launch.via), ("org.gnome.Nautilus.desktop", "entry-id"))

    def test_terminal_keeps_shell_folder(self):
        launch = self.launch("foot", ProcInfo(shell_cwd="/home/me/src"))
        self.assertEqual((launch.how, launch.entry, launch.cwd), ("terminal", "foot.desktop", "/home/me/src"))

    def test_systemd_unit(self):
        launch = self.launch("some-odd-class", ProcInfo(unit="app-Hyprland-org.gnome.Nautilus-1234.scope"))
        self.assertEqual((launch.entry, launch.via), ("org.gnome.Nautilus.desktop", "systemd-unit"))

    def test_config_mapping_wins(self):
        hotkeys = "chrome-__home_me_.local_share_omarchy-hotkeys_hotkeys.html-Default"
        resolver = Resolver(self.entries, {hotkeys: "omarchy-hotkeys.desktop"}, ["zoom"])
        launch = self.launch(hotkeys, resolver=resolver)
        self.assertEqual((launch.entry, launch.via), ("omarchy-hotkeys.desktop", "config"))
        # The ".desktop" suffix is optional in the config.
        resolver = Resolver(self.entries, {hotkeys: "omarchy-hotkeys"})
        self.assertEqual(self.launch(hotkeys, resolver=resolver).entry, "omarchy-hotkeys.desktop")

    def test_config_mapping_to_missing_app_is_a_problem(self):
        resolver = Resolver(self.entries, {"x": "nope.desktop"})
        self.assertIn("isn't installed", resolver.resolve("x").problem)

    def test_unknown_app_is_a_problem_not_a_guess(self):
        result = self.resolver.resolve("chrome-__home_me_.local_share_omarchy-hotkeys_hotkeys.html-Default",
                                       ProcInfo(unit="app-org.chromium.Chromium-10368.scope"))
        self.assertIsNone(result.launch)
        self.assertIn("map it under [apps]", result.problem)

    def test_never_restore_by_class_and_by_app(self):
        self.assertTrue(self.resolver.resolve("zoom").skip)
        self.assertTrue(self.resolver.resolve("chrome-app.zoom.us__wc-Default").skip)
        resolver = Resolver(self.entries, {"meeting": "Zoom.desktop"}, ["zoom"])
        self.assertIn("Zoom", resolver.resolve("meeting").skip)

    def test_window_without_class_is_skipped(self):
        self.assertTrue(self.resolver.resolve("").skip)


class Units(unittest.TestCase):
    def test_unit_names(self):
        self.assertEqual(ids_from_unit("app-org.chromium.Chromium-10368.scope"), ["org.chromium.Chromium"])
        self.assertEqual(ids_from_unit("app-Hyprland-foot-1234.scope"), ["Hyprland-foot", "foot"])
        self.assertEqual(ids_from_unit("app-Hyprland-org.gnome.Nautilus@a1b2.service"),
                         ["Hyprland-org.gnome.Nautilus", "org.gnome.Nautilus"])
        self.assertEqual(ids_from_unit(r"app-my\x2dapp-99.scope")[0], "my-app")
        self.assertEqual(ids_from_unit("session-2.scope"), [])
        self.assertEqual(ids_from_unit(None), [])

    def test_parse_ignores_translations_and_other_groups(self):
        values = parse_desktop_entry("[Desktop Entry]\nName=Files\nName[de]=Dateien\n"
                                     "[Desktop Action x]\nExec=other\n")
        self.assertEqual(values, {"Name": "Files"})


class Proc(unittest.TestCase):
    """A fake /proc: terminal 100 runs shell 205 (newest child) and helper 150."""

    def make(self, shell_session=205, shell_tty=34817):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "100/task/100").mkdir(parents=True)
        (root / "100/cgroup").write_text("0::/user.slice/user-1000.slice/user@1000.service/"
                                         "app.slice/app-Hyprland-foot-77.scope\n")
        (root / "100/task/100/children").write_text("150 205 ")
        for pid, session, tty in ((150, 1, 0), (205, shell_session, shell_tty)):
            (root / str(pid)).mkdir()
            (root / str(pid) / "stat").write_text(f"{pid} (some (odd) name) S 100 {pid} {session} {tty} 0 0\n")
            os.symlink(f"/home/me/dir{pid}", root / str(pid) / "cwd")
        return root

    def test_unit_and_shell_folder(self):
        info = read_proc(100, self.make())
        self.assertEqual(info.unit, "app-Hyprland-foot-77.scope")
        self.assertEqual(info.shell_cwd, "/home/me/dir205")

    def test_child_that_is_not_a_terminal_shell(self):
        self.assertIsNone(read_proc(100, self.make(shell_tty=0)).shell_cwd)
        self.assertIsNone(read_proc(100, self.make(shell_session=1)).shell_cwd)

    def test_gone_process(self):
        self.assertEqual(read_proc(4242, self.make()), ProcInfo())
        self.assertEqual(read_proc(0), ProcInfo())


if __name__ == "__main__":
    unittest.main()
