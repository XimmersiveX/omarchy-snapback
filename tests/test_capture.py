import unittest
from pathlib import Path

from snapback.capture import Source, capture
from snapback.geometry import project

FIXTURES = Path(__file__).parent / "fixtures"
TV = "LG Electronics LG TV SSCR2 SERIAL3"
PORTRAIT = "Ancor Communications Inc ROG PG279Q #SERIAL1"


class DeskWithTvOn(unittest.TestCase):
    """Recorded from the real desk: TV on, two windows on the TV, one on the portrait ROG."""

    def setUp(self):
        self.source = Source.from_dir(FIXTURES / "tv-on-3-windows")
        self.windows = capture(self.source)

    def test_every_window_has_an_owner_and_slot(self):
        self.assertEqual(len(self.windows), 3)
        for w in self.windows:
            self.assertIsNotNone(w.owner, w.app)
            self.assertIsNotNone(w.placement, w.app)

    def test_owners_match_the_screens(self):
        owners = sorted((w.owner, w.slot) for w in self.windows)
        self.assertEqual(owners, [(PORTRAIT, 1), (TV, 1), (TV, 1)])

    def test_windows_are_shown_on_their_owner(self):
        for w in self.windows:
            self.assertEqual(w.shown_on, w.owner)

    def test_every_window_projects_back_to_where_it_was(self):
        monitors = {m.identity: m for m in self.source.monitors()}
        clients = {c["address"]: c for c in self.source.snapshot["clients"]}
        for w in self.windows:
            rect = project(w.placement, monitors[w.shown_on])
            client = clients[w.address]
            self.assertEqual((rect.x, rect.y, rect.w, rect.h), (*client["at"], *client["size"]))


if __name__ == "__main__":
    unittest.main()
