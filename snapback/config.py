"""Snapback's user config, ~/.config/snapback/config.toml. Optional.

    # Windows Snapback never reopens, matched case-insensitively anywhere in the
    # window class, the desktop entry id or the app's name. Setting this
    # replaces the default, which is ["zoom"] so a restore never rejoins a call.
    never_restore = ["zoom", "1password"]

    # Window classes no desktop entry claims, mapped to the entry that opens them.
    [apps]
    "chrome-__home_me_.local_share_omarchy-hotkeys_hotkeys.html-Default" = "omarchy-hotkeys.desktop"
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import files

DEFAULT_NEVER_RESTORE = ["zoom"]


class ConfigError(ValueError):
    pass


@dataclass
class Config:
    never_restore: list[str] = field(default_factory=lambda: list(DEFAULT_NEVER_RESTORE))
    apps: dict[str, str] = field(default_factory=dict)


def config_path() -> Path:
    return files.config_home() / "snapback" / "config.toml"


def load(path: Path | None = None) -> Config:
    path = path or config_path()
    try:
        data = tomllib.loads(path.read_text())
    except FileNotFoundError:
        return Config()
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc

    config = Config()
    if "never_restore" in data:
        never = data["never_restore"]
        if not isinstance(never, list) or not all(isinstance(p, str) for p in never):
            raise ConfigError(f"{path}: never_restore must be a list of strings")
        config.never_restore = never
    apps = data.get("apps", {})
    if not isinstance(apps, dict) or not all(isinstance(v, str) for v in apps.values()):
        raise ConfigError(f"{path}: [apps] must map window classes to desktop entry ids")
    config.apps = dict(apps)
    return config
