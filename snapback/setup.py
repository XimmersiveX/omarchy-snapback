"""Hook Snapback into login: restore the last session, then keep it saved.

Omarchy runs every file in ~/.config/omarchy/hooks/post-boot.d/ with bash
shortly after login, one after another. Snapback's hook hands straight over
to a systemd user unit, `snapback`, so the hooks after it aren't held up and
the watcher isn't tied to whatever started it. Apps a restore opens go into
their own scopes through uwsm-app, so stopping the unit never closes them.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

from . import files

UNIT = "snapback"
MARKER = "# Added by `snapback setup`"


def hook_path() -> Path:
    return files.config_home() / "omarchy" / "hooks" / "post-boot.d" / "snapback"


def package_root() -> Path:
    """The folder holding the snapback package, for PYTHONPATH."""
    return Path(__file__).resolve().parent.parent


def unit_command(command: str, root: Path | None = None) -> list[str]:
    """The unit runs `snapback <command>`, logging what it does to the journal
    (journalctl --user -u snapback)."""
    root = root or package_root()
    return ["systemd-run", "--user", f"--unit={UNIT}", "--collect", "--quiet",
            "--property=Type=exec", "--setenv=PATH", "--setenv=HYPRLAND_INSTANCE_SIGNATURE",
            "--setenv=XDG_RUNTIME_DIR", f"--setenv=PYTHONPATH={root}",
            "--setenv=PYTHONDONTWRITEBYTECODE=1",  # the plugin reloads when its files change
            # -P: never import from the current folder, so a stray ~/json.py
            # (or a clone at ~/snapback) can't stand in for Snapback's code.
            "python3", "-P", "-m", "snapback", command, "--verbose"]


def hook_text(root: Path | None = None) -> str:
    root = root or package_root()
    # Removing the plugin deletes its folder but not this hook (Omarchy runs
    # no plugin code on removal), so the hook removes itself once Snapback is gone.
    return ("#!/bin/bash\n"
            "# Snapback: put the last session back after login, then keep it saved.\n"
            f"{MARKER}; remove it with `snapback setup --remove`.\n"
            "# Once Snapback itself has been removed, this file deletes itself.\n"
            f'[[ -d {shlex.quote(str(root / "snapback"))} ]] || {{ rm -f -- "$0"; exit 0; }}\n'
            + shlex.join(unit_command("login", root)) + "\n")


def install(path: Path | None = None, root: Path | None = None) -> Path:
    path = path or hook_path()
    if path.exists() and MARKER not in path.read_text():
        raise FileExistsError(f"{path} exists and wasn't written by Snapback; leaving it alone")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(hook_text(root))
    path.chmod(0o755)
    return path


def remove(path: Path | None = None) -> bool:
    path = path or hook_path()
    if not path.exists():
        return False
    if MARKER not in path.read_text():
        raise FileExistsError(f"{path} wasn't written by Snapback; leaving it alone")
    path.unlink()
    return True


def unit_running() -> bool:
    result = subprocess.run(["systemctl", "--user", "is-active", "--quiet", UNIT], check=False)
    return result.returncode == 0


def start(command: str) -> None:
    subprocess.run(unit_command(command), check=True)


def stop() -> None:
    subprocess.run(["systemctl", "--user", "stop", UNIT], check=False)
