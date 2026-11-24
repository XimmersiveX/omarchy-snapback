"""Adapter for Omarchy. Every read of Omarchy's files goes through this module.

Omarchy 4.5 gives each physical monitor its own block of ten workspaces
(block 0 = 1-10, block 1 = 11-20, ...), keyed by the monitor's identity, and
keeps the assignment in a state file. When a monitor is unplugged, its
workspaces move into another monitor's block, and a second file records where
each one came from. See default/hypr/monitor-workspaces.lua in Omarchy.

File formats (tab-separated, one entry per line):
    monitor-workspaces          <block>  <connector or empty>  <identity>
    monitor-workspaces-merged   <current ws id>  <original ws id>  <identity>

Where they live under ~/.local/state/omarchy/:
    Omarchy 4.5:    monitor-workspaces, monitor-workspaces-merged
    later versions: monitors/workspaces, monitors/workspaces-merged
Migration 1793461630 moves the old pair to the new place and lets the old pair
win when both exist, so read the old pair if it is there and the new one if not.
Snapback's own fixtures always use the old names.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

SLOTS = 10
BLOCKS_FILE = "monitor-workspaces"
MERGED_FILE = "monitor-workspaces-merged"
LAYOUTS = (
    (BLOCKS_FILE, MERGED_FILE),
    ("monitors/workspaces", "monitors/workspaces-merged"),
)


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local/state")
    return Path(base) / "omarchy"


def state_files(directory: Path | None = None) -> tuple[Path, Path]:
    """Omarchy's (blocks, merged) files in `directory`, in whichever layout is there.

    When neither layout has a blocks file, the newest layout's paths are
    returned, which don't exist.
    """
    directory = directory or state_dir()
    for blocks, merged in LAYOUTS:
        if (directory / blocks).exists():
            return directory / blocks, directory / merged
    blocks, merged = LAYOUTS[-1]
    return directory / blocks, directory / merged


@dataclass
class WorkspaceState:
    """Omarchy's per-monitor workspace records."""

    blocks: dict[str, int] = field(default_factory=dict)       # identity -> block
    connectors: dict[str, str] = field(default_factory=dict)   # connector -> identity, plugged-in only
    merged: dict[int, tuple[int, str]] = field(default_factory=dict)  # ws -> (original ws, identity)
    found: bool = False  # False when Omarchy's file was missing

    @classmethod
    def parse(cls, blocks_text: str | None, merged_text: str | None) -> "WorkspaceState":
        state = cls(found=blocks_text is not None)
        for line in (blocks_text or "").splitlines():
            parts = line.split("\t")
            if len(parts) == 3 and parts[0].isdecimal() and parts[2]:
                state.blocks[parts[2]] = int(parts[0])
                if parts[1]:
                    state.connectors[parts[1]] = parts[2]
        for line in (merged_text or "").splitlines():
            parts = line.split("\t")
            if len(parts) == 3 and parts[0].isdecimal() and parts[1].isdecimal() and parts[2]:
                state.merged[int(parts[0])] = (int(parts[1]), parts[2])
        return state

    @classmethod
    def load(cls, directory: Path | None = None) -> "WorkspaceState":
        blocks, merged = state_files(directory)
        return cls.parse(_read(blocks), _read(merged))

    def owner(self, workspace_id: int) -> tuple[str, int] | None:
        """The (monitor identity, slot 1-10) a regular workspace belongs to.

        A workspace that was merged onto another monitor still belongs to the
        monitor it came from, under its original slot.
        """
        if workspace_id < 1:
            return None
        if workspace_id in self.merged:
            original, identity = self.merged[workspace_id]
            return identity, _slot(original)
        block = (workspace_id - 1) // SLOTS
        for identity, owned in self.blocks.items():
            if owned == block:
                return identity, _slot(workspace_id)
        return None

    def workspace_for(self, identity: str, slot: int) -> int | None:
        """The global workspace id for a monitor's slot, if Omarchy knows the monitor."""
        block = self.blocks.get(identity)
        if block is None or not 1 <= slot <= SLOTS:
            return None
        return block * SLOTS + slot


def _slot(workspace_id: int) -> int:
    return (workspace_id - 1) % SLOTS + 1


def _read(path: Path) -> str | None:
    try:
        return path.read_text()
    except FileNotFoundError:
        return None


def installed_version() -> str | None:
    """Omarchy's version, e.g. "4.5.0-3", or None when it can't be read."""
    try:
        result = subprocess.run(["omarchy", "version"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = result.stdout.strip().splitlines()
    return lines[0].strip() if result.returncode == 0 and lines else None
