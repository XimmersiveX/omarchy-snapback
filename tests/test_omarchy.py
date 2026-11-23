import tempfile
import unittest
from pathlib import Path

from snapback.omarchy import WorkspaceState

BLOCKS = (
    "0\tHDMI-A-1\tLG TV SERIAL3\n"
    "1\tDP-2\tROG PG279Q #SERIAL1\n"
    "2\tDP-3\tROG PG279Q #SERIAL2\n"
)


class Blocks(unittest.TestCase):
    def setUp(self):
        self.state = WorkspaceState.parse(BLOCKS, "")

    def test_parse(self):
        self.assertTrue(self.state.found)
        self.assertEqual(self.state.blocks["ROG PG279Q #SERIAL2"], 2)
        self.assertEqual(self.state.connectors["DP-2"], "ROG PG279Q #SERIAL1")

    def test_owner_and_slot(self):
        self.assertEqual(self.state.owner(1), ("LG TV SERIAL3", 1))
        self.assertEqual(self.state.owner(10), ("LG TV SERIAL3", 10))
        self.assertEqual(self.state.owner(11), ("ROG PG279Q #SERIAL1", 1))
        self.assertEqual(self.state.owner(23), ("ROG PG279Q #SERIAL2", 3))

    def test_unknown_block_and_special_workspaces(self):
        self.assertIsNone(self.state.owner(31))
        self.assertIsNone(self.state.owner(-98))

    def test_round_trip(self):
        for ws in range(1, 31):
            self.assertEqual(self.state.workspace_for(*self.state.owner(ws)), ws)

    def test_unknown_monitor_has_no_workspace(self):
        self.assertIsNone(self.state.workspace_for("New Monitor", 1))
        self.assertIsNone(self.state.workspace_for("LG TV SERIAL3", 11))


class Merged(unittest.TestCase):
    def test_tv_workspace_parked_on_main_rog_still_belongs_to_tv(self):
        # TV unplugged: its ws 2 now lives as ws 24 in the main ROG's block.
        state = WorkspaceState.parse(BLOCKS.replace("HDMI-A-1", ""), "24\t2\tLG TV SERIAL3\n")
        self.assertEqual(state.owner(24), ("LG TV SERIAL3", 2))
        self.assertEqual(state.workspace_for("LG TV SERIAL3", 2), 2)
        self.assertNotIn("HDMI-A-1", state.connectors)


class Missing(unittest.TestCase):
    def test_no_omarchy_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = WorkspaceState.load(Path(tmp))
        self.assertFalse(state.found)
        self.assertIsNone(state.owner(1))

    def test_garbage_lines_are_skipped(self):
        state = WorkspaceState.parse("nonsense\n0\tDP-1\n\n1\t\tReal Monitor\n", "x\ty\tz\n")
        self.assertEqual(state.blocks, {"Real Monitor": 1})
        self.assertEqual(state.merged, {})


class Layouts(unittest.TestCase):
    """Omarchy 4.5 keeps the files in omarchy/; later versions in omarchy/monitors/."""

    def write(self, root, blocks, merged, text_blocks, text_merged):
        for name, text in ((blocks, text_blocks), (merged, text_merged)):
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)

    def test_new_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write(root, "monitors/workspaces", "monitors/workspaces-merged", BLOCKS, "13\t1\tLG TV SERIAL3\n")
            state = WorkspaceState.load(root)
        self.assertTrue(state.found)
        self.assertEqual(state.blocks["LG TV SERIAL3"], 0)
        self.assertEqual(state.owner(13), ("LG TV SERIAL3", 1))

    def test_old_layout_wins_until_migrated(self):
        # Omarchy's migration lets the old files win: anything under monitors/
        # before it ran was written from nothing by a config reload.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write(root, "monitor-workspaces", "monitor-workspaces-merged", BLOCKS, "13\t1\tLG TV SERIAL3\n")
            self.write(root, "monitors/workspaces", "monitors/workspaces-merged", "0\tDP-2\tSomething Else\n", "")
            state = WorkspaceState.load(root)
        self.assertEqual(state.blocks["LG TV SERIAL3"], 0)
        self.assertNotIn("Something Else", state.blocks)
        self.assertEqual(state.owner(13), ("LG TV SERIAL3", 1))

    def test_merged_comes_from_the_same_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write(root, "monitors/workspaces", "monitors/workspaces-merged", BLOCKS, "")
            (root / "monitor-workspaces-merged").write_text("13\t1\tLG TV SERIAL3\n")
            state = WorkspaceState.load(root)
        self.assertEqual(state.merged, {})


if __name__ == "__main__":
    unittest.main()
