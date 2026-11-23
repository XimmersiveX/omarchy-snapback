"""The shape of a tiled workspace, and how to build it again.

Hyprland's dwindle layout is a tree of splits: each split cuts a box into two,
side by side or one above the other, and each leaf is a window. Hyprland never
reports the tree, but saved tile positions show it: a vertical line that no
tile crosses is a side-by-side split, a horizontal one a stacked split, and so
on inside each half.

Dwindle adds a new window by splitting the focused window, on the side
`preselect` names. So a tree is rebuilt by starting from one window and, for
each split, focusing the window that holds that box and bringing in the first
window of the other half on the right side or below.

Boxes here are fractions of their monitor, (x0, y0, x1, y1), so a shape saved
on one monitor compares with the same shape on another.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Hashable

Box = tuple[float, float, float, float]

# Tiles are separated by gaps; a cut may sit anywhere in the gap. As a
# fraction of the monitor, this comfortably covers Omarchy's gaps.
GAP = 0.02
SAME_SHAPE = 0.03


@dataclass(frozen=True)
class Split:
    side_by_side: bool        # True: first | second; False: first above second
    first: "Node"
    second: "Node"


Node = Hashable  # a leaf is the window's key; a Split is a split


def split_tree(boxes: dict[Hashable, Box]) -> Node | None:
    """The split tree that tiles `boxes`, or None if no tree of splits does.

    Where several cuts would do (three columns), the one nearest the middle
    wins, as dwindle splits boxes in half until resized.
    """
    keys = list(boxes)
    if not keys:
        return None
    if len(keys) == 1:
        return keys[0]

    x0 = min(boxes[k][0] for k in keys)
    y0 = min(boxes[k][1] for k in keys)
    x1 = max(boxes[k][2] for k in keys)
    y1 = max(boxes[k][3] for k in keys)
    best = None
    for side_by_side in (True, False):
        lo, hi = (0, 2) if side_by_side else (1, 3)
        start, end = (x0, x1) if side_by_side else (y0, y1)
        for cut in sorted({boxes[k][hi] for k in keys}):
            first = [k for k in keys if boxes[k][hi] <= cut + GAP / 2]
            second = [k for k in keys if boxes[k][lo] >= cut - GAP / 2]
            if not first or not second or len(first) + len(second) != len(keys) or set(first) & set(second):
                continue
            off_middle = abs((cut - start) / (end - start) - 0.5)
            if best is None or off_middle < best[0] - 1e-9:
                best = (off_middle, side_by_side, first, second)
    if best is None:
        return None
    _, side_by_side, first, second = best
    a = split_tree({k: boxes[k] for k in first})
    b = split_tree({k: boxes[k] for k in second})
    if a is None or b is None:
        return None
    return Split(side_by_side, a, b)


def first_leaf(node: Node) -> Hashable:
    while isinstance(node, Split):
        node = node.first
    return node


def leaves(node: Node) -> list[Hashable]:
    if isinstance(node, Split):
        return leaves(node.first) + leaves(node.second)
    return [node]


def build_steps(tree: Node) -> tuple[Hashable, list[tuple[Hashable, str, Hashable]]]:
    """(the window to start from, [(window to split, "r" or "d", window to add)]).

    Adding each window beside or under the one named, in order, from a
    workspace holding only the starting window, gives back `tree`.
    """
    steps: list[tuple[Hashable, str, Hashable]] = []

    def build(node: Node, anchor: Hashable) -> None:
        if not isinstance(node, Split):
            return
        added = first_leaf(node.second)
        steps.append((anchor, "r" if node.side_by_side else "d", added))
        build(node.first, anchor)
        build(node.second, added)

    start = first_leaf(tree)
    build(tree, start)
    return start, steps


def same_shape(saved: dict[Hashable, Box], now: dict[Hashable, Box], tolerance: float = SAME_SHAPE) -> bool:
    """Whether every window's tile is where it was saved, within `tolerance`."""
    return saved.keys() == now.keys() and all(
        abs(a - b) <= tolerance for k in saved for a, b in zip(saved[k], now[k]))
