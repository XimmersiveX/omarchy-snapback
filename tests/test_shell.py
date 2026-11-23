"""The Omarchy panel's pure helpers (shell/Model.js), run with node when it's there."""

import shutil
import subprocess
import unittest
from pathlib import Path

HERE = Path(__file__).parent


@unittest.skipUnless(shutil.which("node"), "node isn't installed")
class Model(unittest.TestCase):
    def test_model(self):
        result = subprocess.run(["node", str(HERE / "js" / "model.test.js")],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
