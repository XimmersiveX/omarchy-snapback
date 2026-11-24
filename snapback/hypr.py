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


def query(what: str, *args: str) -> object:
    """Run `hyprctl -j <what> [args]` and return the parsed JSON."""
    try:
        result = subprocess.run(
            ["hyprctl", "-j", what, *args],
            capture_output=True, text=True, errors="replace", timeout=5, check=True,
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


def lua(value: object) -> str:
    """A Python value as a Lua literal: numbers, booleans, strings, flat tables."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, dict):
        return "{" + ",".join(f"{k}={lua(v)}" for k, v in value.items()) + "}"
    text = str(value)
    out = []
    for ch in text:
        if ch in '\\"':
            out.append("\\" + ch)
        elif ord(ch) < 32 or ord(ch) == 127:
            out.append(f"\\{ord(ch):03d}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def evaluate(code: str) -> None:
    """Run Lua inside Hyprland. Hyprland answers "ok", or the error."""
    try:
        result = subprocess.run(["hyprctl", "eval", code],
                                capture_output=True, text=True, errors="replace", timeout=5)
    except FileNotFoundError as exc:
        raise HyprError("hyprctl not found; is Hyprland installed?") from exc
    except subprocess.TimeoutExpired as exc:
        raise HyprError("hyprctl eval timed out") from exc
    answer = (result.stdout + result.stderr).strip()
    if result.returncode != 0 or answer != "ok":
        raise HyprError(f"Hyprland refused {code!r}: {answer or result.returncode}")


def dispatch(method: str, **args: object) -> None:
    """hl.dispatch(hl.dsp.<method>({...})), e.g. dispatch("window.float", window=..., action="on")."""
    evaluate(f"hl.dispatch(hl.dsp.{method}({lua(args)}))")


def option(name: str) -> object:
    """A Hyprland option's value, e.g. option("misc:focus_on_activate") -> True."""
    data = query("getoption", name)
    for key in ("bool", "int", "float", "str"):
        if isinstance(data, dict) and key in data:
            return data[key]
    raise HyprError(f"can't read Hyprland option {name}")
