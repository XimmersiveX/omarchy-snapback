"""Adapter for Hyprland. Every call to hyprctl goes through this module.

Snapback reads Hyprland's JSON and computes placement itself, so when Hyprland
changes, this is the only file that should need fixing.
"""

from __future__ import annotations

import json
import subprocess

QUERIES = ("version", "monitors", "clients", "workspaces")


class HyprError(RuntimeError):
    pass


def query(what: str) -> object:
    """Run `hyprctl -j <what>` and return the parsed JSON."""
    try:
        result = subprocess.run(
            ["hyprctl", "-j", what],
            capture_output=True, text=True, timeout=5, check=True,
        )
    except FileNotFoundError as exc:
        raise HyprError("hyprctl not found; is Hyprland installed?") from exc
    except subprocess.CalledProcessError as exc:
        raise HyprError(f"hyprctl {what} failed: {exc.stderr.strip()}") from exc
    except subprocess.TimeoutExpired as exc:
        raise HyprError(f"hyprctl {what} timed out") from exc
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise HyprError(f"hyprctl {what} returned non-JSON output") from exc


def snapshot() -> dict:
    """Everything Snapback reads from Hyprland, in one dict.

    The same shape is stored in test fixtures, so code that takes a snapshot
    works the same on live data and recorded data.
    """
    return {name: query(name) for name in QUERIES}
