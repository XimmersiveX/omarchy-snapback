"""Monitors and window geometry.

All coordinates are Hyprland's logical (layout) coordinates: a monitor's `x`/`y`
are logical, but its `width`/`height` are the raw mode in pixels, before scale
and rotation. A window's `at`/`size` are logical.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Rect:
    x: float
    y: float
    w: float
    h: float

    @property
    def right(self) -> float:
        return self.x + self.w

    @property
    def bottom(self) -> float:
        return self.y + self.h

    @property
    def center(self) -> tuple[float, float]:
        return self.x + self.w / 2, self.y + self.h / 2

    def rounded(self) -> "Rect":
        return Rect(round(self.x), round(self.y), round(self.w), round(self.h))


@dataclass(frozen=True)
class Monitor:
    name: str          # connector, e.g. DP-2; can change between plugs
    identity: str      # make + model + serial, as Omarchy names it
    box: Rect          # logical area in the global layout
    usable: Rect       # box minus reserved space (the bar)
    scale: float
    transform: int


def logical_size(width: int, height: int, scale: float, transform: int) -> tuple[float, float]:
    """A mode's size in layout coordinates. Odd transforms rotate 90 or 270 degrees."""
    w, h = width / scale, height / scale
    if transform % 2 == 1:
        w, h = h, w
    return round(w, 3), round(h, 3)


def monitors_from_hypr(raw: list[dict], known: dict[str, str] | None = None) -> list[Monitor]:
    """Build monitors from `hyprctl -j monitors`, naming each like Omarchy does.

    `known` maps connector -> identity from Omarchy's state file and wins when
    it agrees with the monitor's description, so Snapback and Omarchy always
    use the same name. Otherwise the identity is the description (make, model,
    serial), or the connector when the description is empty, and two monitors
    reporting the same description get " #2", " #3" by connector order.
    """
    taken: set[str] = set()
    identities: dict[str, str] = {}
    for mon in raw:
        identity = (known or {}).get(mon["name"])
        base = mon.get("description") or mon["name"]
        if identity and identity not in taken and _strip_count(identity) == base:
            identities[mon["name"]] = identity
            taken.add(identity)
    for mon in sorted(raw, key=lambda m: m["name"]):
        if mon["name"] in identities:
            continue
        base = mon.get("description") or mon["name"]
        identity, count = base, 1
        while identity in taken:
            count += 1
            identity = f"{base} #{count}"
        taken.add(identity)
        identities[mon["name"]] = identity

    monitors = []
    for mon in raw:
        if mon.get("disabled"):
            continue
        w, h = logical_size(mon["width"], mon["height"], mon["scale"], mon.get("transform", 0))
        box = Rect(mon["x"], mon["y"], w, h)
        left, top, right, bottom = (mon.get("reserved") or [0, 0, 0, 0])[:4]
        usable = Rect(box.x + left, box.y + top, box.w - left - right, box.h - top - bottom)
        monitors.append(Monitor(mon["name"], identities[mon["name"]], box, usable,
                                mon["scale"], mon.get("transform", 0)))
    return monitors


def _strip_count(identity: str) -> str:
    """'X #2' -> 'X'. Only a numeric suffix is a count; '#ASOC9Yu6DtPd' is a serial."""
    head, sep, tail = identity.rpartition(" #")
    return head if sep and tail.isdigit() else identity


@dataclass(frozen=True)
class Placement:
    """A window's geometry stored relative to the monitor it was on."""

    dx: float           # offset from the monitor's top-left, logical
    dy: float
    w: float
    h: float
    monitor_w: float    # that monitor's logical size when saved
    monitor_h: float

    def to_dict(self) -> dict:
        return {"dx": self.dx, "dy": self.dy, "w": self.w, "h": self.h,
                "monitor_w": self.monitor_w, "monitor_h": self.monitor_h}

    @classmethod
    def from_dict(cls, data: dict) -> "Placement":
        return cls(**{k: float(data[k]) for k in ("dx", "dy", "w", "h", "monitor_w", "monitor_h")})


def relative(window: Rect, monitor: Monitor) -> Placement:
    return Placement(window.x - monitor.box.x, window.y - monitor.box.y,
                     window.w, window.h, monitor.box.w, monitor.box.h)


SAME_SIZE_TOLERANCE = 1.0


def project(placement: Placement, target: Monitor) -> Rect:
    """Where a saved window goes on `target`.

    If the target is the same size as the saved monitor, the window goes back
    to exactly the same spot. Otherwise (a different monitor, or the same one
    at another scale or rotation) it keeps its logical size when that fits and
    shrinks proportionally when it doesn't, and its center lands at the same
    relative position. Either way the result is kept inside the usable area.
    """
    same_size = (abs(placement.monitor_w - target.box.w) <= SAME_SIZE_TOLERANCE
                 and abs(placement.monitor_h - target.box.h) <= SAME_SIZE_TOLERANCE)
    if same_size:
        rect = Rect(target.box.x + placement.dx, target.box.y + placement.dy,
                    placement.w, placement.h)
    else:
        area = target.usable
        fit = min(1.0, area.w / placement.w, area.h / placement.h)
        w, h = placement.w * fit, placement.h * fit
        cx = (placement.dx + placement.w / 2) / placement.monitor_w
        cy = (placement.dy + placement.h / 2) / placement.monitor_h
        rect = Rect(target.box.x + cx * target.box.w - w / 2,
                    target.box.y + cy * target.box.h - h / 2, w, h)
    return clamp(rect, target.usable).rounded()


def clamp(rect: Rect, area: Rect) -> Rect:
    """Shrink a rect to fit inside `area`, then slide it fully inside."""
    w, h = min(rect.w, area.w), min(rect.h, area.h)
    x = min(max(rect.x, area.x), area.right - w)
    y = min(max(rect.y, area.y), area.bottom - h)
    return Rect(x, y, w, h)
