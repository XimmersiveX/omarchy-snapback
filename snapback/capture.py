"""Turn a Hyprland snapshot into Snapback's description of each window.

Read-only: nothing here changes the desktop.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from . import hypr
from .geometry import Monitor, Placement, Rect, layout_frame, monitors_from_hypr, relative
from .omarchy import WorkspaceState


@dataclass
class Source:
    """A Hyprland snapshot plus Omarchy's workspace records, live or recorded."""

    snapshot: dict
    workspaces: WorkspaceState
    instance: str | None = None   # Hyprland's instance signature; window ids only mean
                                  # something within one instance

    @classmethod
    def live(cls) -> "Source":
        return cls(hypr.snapshot(), WorkspaceState.load(),
                   os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") or None)

    @classmethod
    def from_dir(cls, directory: Path) -> "Source":
        snapshot = json.loads((directory / "snapshot.json").read_text())
        return cls(snapshot, WorkspaceState.load(directory))

    def monitors(self) -> list[Monitor]:
        return monitors_from_hypr(self.snapshot["monitors"], self.workspaces.connectors)


@dataclass
class CapturedWindow:
    address: str
    app: str                    # initialClass, the most stable name for the app
    title: str
    pid: int
    workspace: str              # "3" for regular, "special:name" for special
    owner: str | None           # identity of the monitor the workspace belongs to
    slot: int | None            # 1-10 within that monitor's block
    shown_on: str | None        # identity of the monitor displaying it right now
    floating: bool
    fullscreen: int
    pinned: bool
    grouped: bool
    placement: Placement | None
    focus: int = -1             # Hyprland's focus history: 0 = most recently focused
    stable_id: str | None = None  # Hyprland's id for the window, never reused within an instance

    def to_dict(self) -> dict:
        data = self.__dict__.copy()
        data["placement"] = self.placement.to_dict() if self.placement else None
        return data


def capture(source: Source) -> list[CapturedWindow]:
    monitors = source.monitors()
    by_id = {m["id"]: m["name"] for m in source.snapshot["monitors"]}
    by_name = {m.name: m for m in monitors}
    windows = []
    for client in source.snapshot["clients"]:
        if not client.get("mapped") or client.get("hidden"):
            continue
        ws = client["workspace"]
        monitor = by_name.get(by_id.get(client.get("monitor")))
        if ws["id"] > 0:
            owner = source.workspaces.owner(ws["id"])
            name = str(ws["id"])
        else:
            owner, name = None, ws["name"]
        rect = Rect(*client["at"], *client["size"])
        frame = monitor
        if monitor and not client.get("floating"):
            frame = layout_frame(rect, monitor, monitors)
        windows.append(CapturedWindow(
            address=client["address"],
            app=app_of(client),
            title=client.get("title", ""),
            pid=client.get("pid", 0),
            workspace=name,
            owner=owner[0] if owner else None,
            slot=owner[1] if owner else None,
            shown_on=monitor.identity if monitor else None,
            floating=bool(client.get("floating")),
            fullscreen=int(client.get("fullscreen") or 0),
            pinned=bool(client.get("pinned")),
            grouped=bool(client.get("grouped")),
            placement=relative(rect, frame) if frame else None,
            focus=int(client.get("focusHistoryID", -1)),
            stable_id=client.get("stableId"),
        ))
    return windows


def app_of(client: dict) -> str:
    """A Hyprland client's app: its initialClass, the most stable name for it."""
    return client.get("initialClass") or client.get("class") or ""
