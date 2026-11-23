"""The Omarchy panel's own decisions, run in an offscreen Quickshell.

tests/qml/shell.qml drives the real shell/Panel.qml with the command line
replaced by `true`. Needs quickshell and Omarchy's shell modules (qs.Commons,
qs.Ui); skipped where they aren't installed, so the rest of the suite runs
anywhere.
"""

import os
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHELL = Path(os.environ.get("OMARCHY_SHELL", "/usr/share/omarchy/shell"))
ANSI = re.compile(r"\x1b\[[0-9;]*m")
# Lines Quickshell prints when QML goes wrong at run time.
QML_ERROR = re.compile(r"(ReferenceError|TypeError|Unable to assign|is not a type|"
                       r"Cannot assign|Type \w+ unavailable|Error:)")


def run_harness() -> str:
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    for name in ("WAYLAND_DISPLAY", "DISPLAY", "QT_QPA_PLATFORMTHEME"):
        env.pop(name, None)  # the gtk3 theme opens a display of its own
    with tempfile.TemporaryDirectory(prefix="snapback-qml-") as directory:
        harness = Path(directory)
        shutil.copy(ROOT / "tests/qml/shell.qml", harness / "shell.qml")
        for name in ("Service.qml", "Model.js"):
            shutil.copy(ROOT / "shell" / name, harness / name)
        shutil.copytree(ROOT / "shell/i18n", harness / "i18n")
        # Panel.qml extends qs.Ui's Panel; beside it under its own name it
        # would resolve to itself.
        shutil.copy(ROOT / "shell/Panel.qml", harness / "BarPanel.qml")
        (harness / "Commons").symlink_to(SHELL / "Commons")
        shutil.copytree(SHELL / "Ui", harness / "Ui", symlinks=True)
        shutil.copy(ROOT / "tests/qml/KeyboardPanel.qml", harness / "Ui/KeyboardPanel.qml")
        runtime = harness / "runtime"
        runtime.mkdir(mode=0o700)
        env["XDG_RUNTIME_DIR"] = str(runtime)
        shell = subprocess.Popen(["quickshell", "-p", str(harness)], stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, env=env)
        timer = threading.Timer(30, shell.kill)
        timer.start()
        lines = []
        for line in shell.stdout:
            lines.append(ANSI.sub("", line))
            if re.search(r"qml:\s+(ALL OK|\d+ FAILED)", lines[-1]):
                break
        timer.cancel()
        shell.kill()
        shell.wait()
        shell.stdout.close()
    return "".join(lines)


@unittest.skipUnless(shutil.which("quickshell") and SHELL.is_dir(),
                     "needs quickshell and Omarchy's shell modules")
class PanelLogic(unittest.TestCase):
    def test_panel(self):
        output = run_harness()
        checks = re.findall(r"qml:\s+(ok|FAIL)\s+(.*?)\s*$", output, re.MULTILINE)
        self.assertTrue(checks, "the harness ran no checks:\n" + output[-3000:])
        failed = [name for state, name in checks if state == "FAIL"]
        self.assertEqual(failed, [], output[-3000:])
        self.assertIn("ALL OK", output, output[-3000:])
        errors = [line for line in output.splitlines()
                  if QML_ERROR.search(line) and "/Ui/" not in line and "/Commons/" not in line]
        self.assertEqual(errors, [], "QML errors in the panel")
