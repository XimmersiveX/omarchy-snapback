"""Which screen is which, when a setup meets a different set of screens.

A setup remembers each window's physical monitor. At home those monitors are
there and every window goes back to its own. Elsewhere (at work, or the
laptop alone at a coffee shop) they aren't, so screens are matched by role:

    * Each place has a main screen, marked once per physical monitor (or,
      until then, guessed: the only screen, else the largest, else the
      leftmost of the largest).
    * When the setup's own main screen is plugged in, this is the place the
      setup was made: screens that are missing (a TV in standby) keep their
      windows waiting, and Omarchy sends them home when the screen is back.
    * Otherwise the setup's main screen becomes this place's main screen, and
      its other screens go to the screens on the same side of main, nearest
      first. Screens left over fold into the main screen: their workspaces
      come after the main screen's own, in left-to-right order, as Omarchy
      does with an unplugged monitor's workspaces.

Folding follows Omarchy by default (FOLD_OMARCHY): where Omarchy has already
folded a missing screen's workspace (it remembers where each came from, until
the next login), that screen's windows go into it, so Omarchy still sends them
home when the screen is back. Snapback folds only what Omarchy hasn't, into
workspaces nobody is using. FOLD_SNAPBACK always uses Snapback's order; then
it's a setup, not Omarchy, that puts the windows home again.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from . import files
from .geometry import Monitor, Rect
from .omarchy import SLOTS

FOLD_OMARCHY = "omarchy"
FOLD_SNAPBACK = "snapback"


def marks_path() -> Path:
    return files.state_dir() / "screens.json"


def _load(path: Path | None) -> dict:
    try:
        data = json.loads((path or marks_path()).read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def load_marks(path: Path | None = None) -> set[str]:
    """Identities of the monitors marked as a main screen (one per place)."""
    main = _load(path).get("main")
    return {m for m in main if isinstance(m, str)} if isinstance(main, list) else set()


def load_fold(path: Path | None = None) -> str:
    fold = _load(path).get("fold")
    return fold if fold in (FOLD_OMARCHY, FOLD_SNAPBACK) else FOLD_OMARCHY


def mark_main(identity: str, connected: list[str], path: Path | None = None) -> None:
    """Make `identity` the main screen of the place whose screens are `connected`:
    any other connected screen stops being main; marks from other places stay."""
    _save({"main": sorted((load_marks(path) - set(connected)) | {identity})}, path)


def set_fold(fold: str, path: Path | None = None) -> None:
    if fold not in (FOLD_OMARCHY, FOLD_SNAPBACK):
        raise ValueError(f"fold is {FOLD_OMARCHY!r} or {FOLD_SNAPBACK!r}")
    _save({"fold": fold}, path)


def _save(changes: dict, path: Path | None) -> None:
    path = path or marks_path()
    files.write_json({**_load(path), **changes}, path, sort_keys=True)


def pick_main(screens: list[tuple[str, Rect]], marks: set[str]) -> str | None:
    """The main screen among (identity, box): a marked one, the only one, or
    the largest (leftmost when two are as large)."""
    if not screens:
        return None
    marked = [identity for identity, _ in screens if identity in marks]
    if marked:
        return sorted(marked)[0]
    return min(screens, key=lambda s: (-(s[1].w * s[1].h), s[1].x, s[1].y))[0]


def saved_box(monitor: dict) -> Rect:
    box = monitor.get("box") or [0, 0, 0, 0]
    return Rect(*box)


def saved_main(session: dict, marks: set[str]) -> str | None:
    """The setup's main screen: as recorded when it was saved, else as a mark
    or a guess would pick it among its screens."""
    monitors = [m for m in session.get("monitors", []) if m.get("identity")]
    recorded = [m["identity"] for m in monitors if m.get("main")]
    if recorded:
        return recorded[0]
    return pick_main([(m["identity"], saved_box(m)) for m in monitors], marks)


@dataclass
class Mapping:
    """Saved monitor identity -> today's monitor, and where folded slots go."""

    elsewhere: bool = False                                   # the setup's main screen is absent
    screen: dict[str, Monitor] = field(default_factory=dict)  # saved identity -> monitor (exact or role)
    folded: dict[tuple[str, int], int] = field(default_factory=dict)  # (saved identity, slot) -> main's slot
    main: Monitor | None = None                               # this place's main screen

    def place(self, identity: str | None, slot: int | None) -> tuple[Monitor, int] | None:
        """(monitor, slot) a saved (monitor, slot) goes to, or None to wait."""
        if not identity or not slot:
            return None
        if identity in self.screen:
            return self.screen[identity], slot
        if (identity, slot) in self.folded and self.main:
            return self.main, self.folded[(identity, slot)]
        return None


def _center_x(box: Rect) -> float:
    return box.x + box.w / 2


def map_screens(session: dict, monitors: list[Monitor], marks: set[str],
                omarchy_folded: set[tuple[str, int]] = frozenset(),
                in_use: set[tuple[str, int]] = frozenset()) -> Mapping:
    """How the setup's screens land on today's `monitors`.

    `omarchy_folded` are the saved (identity, slot) Omarchy has folded into a
    workspace that exists now: those keep it, and aren't folded again.
    `in_use` are today's (identity, slot) workspaces that exist: Snapback's own
    folding passes over them rather than add to someone else's workspace."""
    mapping = Mapping()
    by_identity = {m.identity: m for m in monitors}
    saved = [m for m in session.get("monitors", []) if m.get("identity")]
    for m in saved:
        if m["identity"] in by_identity:
            mapping.screen[m["identity"]] = by_identity[m["identity"]]

    home = saved_main(session, marks)
    if home is None or home in by_identity or not monitors:
        return mapping  # the place the setup was made (or nothing to go on)

    mapping.elsewhere = True
    free = [m for m in monitors if m not in mapping.screen.values()]
    here = pick_main([(m.identity, m.box) for m in monitors], marks)
    main = by_identity[here]
    mapping.main = main
    if main in free:
        mapping.screen[home] = main
        free.remove(main)
    # else main already holds an exact match; the setup's main folds into it

    home_box = saved_box(next(m for m in saved if m["identity"] == home))
    unplaced = [m for m in saved if m["identity"] not in mapping.screen]
    for side in (-1, 1):
        mine = sorted((m for m in unplaced
                       if (_center_x(saved_box(m)) - _center_x(home_box)) * side > 0),
                      key=lambda m: abs(_center_x(saved_box(m)) - _center_x(home_box)))
        theirs = sorted((m for m in free if (_center_x(m.box) - _center_x(main.box)) * side > 0),
                        key=lambda m: abs(_center_x(m.box) - _center_x(main.box)))
        for saved_monitor, monitor in zip(mine, theirs):
            mapping.screen[saved_monitor["identity"]] = monitor

    # Whatever is left folds into the main screen, after its own workspaces.
    own_slots = {w.get("slot") for w in session.get("windows", [])
                 if w.get("slot") and mapping.screen.get(w.get("monitor")) is main
                 and (w.get("monitor"), w.get("slot")) not in omarchy_folded}
    next_slot = max(own_slots, default=0) + 1
    leftover = sorted((m for m in saved if m["identity"] not in mapping.screen),
                      key=lambda m: _center_x(saved_box(m)))
    for m in leftover:
        slots = sorted({w["slot"] for w in session.get("windows", [])
                        if w.get("monitor") == m["identity"] and w.get("slot")
                        and (m["identity"], w["slot"]) not in omarchy_folded})
        for slot in slots:
            while next_slot <= SLOTS and (main.identity, next_slot) in in_use:
                next_slot += 1
            if next_slot > SLOTS:
                break  # no room: these wait, as on an unplugged monitor
            mapping.folded[(m["identity"], slot)] = next_slot
            next_slot += 1
    return mapping
