"""Which tabs each browser window has, and how to open a window with them.

Adapter for Chromium-family browsers (Chromium, Google Chrome, Brave). They
keep the open windows and tabs of each profile in `<profile>/Sessions/Session_*`,
an append-only log of commands (SNSS): a tab joins a window, a tab navigates
to a URL, a window or tab closes, and so on. Snapback only reads that file.
It is the browser's own internal format, so it is read defensively: anything
unexpected just means no tabs are known, and a window then comes back the way
it did before Snapback knew about tabs.

Which session window is which Hyprland window: the file has no position under
Wayland, and its titles can't be trusted: a fresh window's aren't written yet
(Chromium writes them later, or never), and a page that changes its own title
keeps the old one there. Both number windows in the order they were made,
though, so with as many windows on each side they are paired in that order.
Otherwise only windows whose selected tab's title is found on just one window
each side are paired.

A window is opened again with `browser --new-window -- URL...`: one window
holding those tabs, added to the browser that is running. `--` ends the
browser's options, and only http, https and file URLs are passed, so a saved
session can't hand the browser an option. A browser that isn't running and
is set to continue where it left off brings back its own last session first
and adds the URLs to it, so it is then started the plain way.
"""

from __future__ import annotations

import json
import os
import re
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Browser:
    app: str          # window class
    folder: str       # under ~/.config
    suffix: str       # appended to the selected tab's title in the window title


BROWSERS = {b.app: b for b in (
    Browser("chromium", "chromium", " - Chromium"),
    Browser("google-chrome", "google-chrome", " - Google Chrome"),
    Browser("brave-browser", "BraveSoftware/Brave-Browser", " - Brave"),
)}

SCHEMES = ("http", "https", "file")
MAX_URL = 8192
MAX_FILE = 64 << 20
PROFILE_RE = re.compile(r"^(Default|Profile \d+)$")

# SNSS command ids (Chromium's session_service_commands.cc).
SET_TAB_WINDOW, SET_TAB_INDEX, UPDATE_NAVIGATION, SELECTED_NAVIGATION = 0, 2, 6, 7
SELECTED_TAB, WINDOW_TYPE, TAB_CLOSED, WINDOW_CLOSED = 8, 9, 16, 17
NORMAL_WINDOW = 0


@dataclass
class SessionWindow:
    id: int
    profile: str
    urls: list[str]          # every tab's current URL, in tab order
    title: str = ""          # the selected tab's title, "" when not written yet


def config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def usable(url: str) -> bool:
    """A URL Snapback will hand a browser: http, https or file, and nothing
    a browser could take for anything else."""
    if not isinstance(url, str) or not url or len(url) > MAX_URL or url.startswith("-"):
        return False
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in url):
        return False
    try:
        return urlsplit(url).scheme in SCHEMES
    except ValueError:
        return False


def origins(urls: Iterable[str]) -> list[str]:
    """What a window's tabs are, for telling windows apart: each tab's site
    (scheme, host and port), so moving around inside a site still counts as
    the same tab."""
    result = []
    for url in urls:
        try:
            parts = urlsplit(url)
            result.append(f"{parts.scheme}://{parts.netloc}".lower() if parts.netloc else url.split("?")[0])
        except ValueError:
            result.append(url)
    return result


def holds(open_urls: list[str], saved_urls: list[str]) -> bool:
    """Whether an open window has every tab a saved window had (by site)."""
    have = origins(open_urls)
    for site in origins(saved_urls):
        if site not in have:
            return False
        have.remove(site)
    return True


def parse(data: bytes, profile: str = "Default") -> list[SessionWindow]:
    """The open tabbed windows in one SNSS session file."""
    if data[:4] != b"SNSS" or len(data) < 8:
        return []
    tab_window: dict[int, int] = {}
    tab_index: dict[int, int] = {}
    navs: dict[int, dict[int, tuple[str, str]]] = {}
    selected_nav: dict[int, int] = {}
    selected_tab: dict[int, int] = {}
    kinds: dict[int, int] = {}
    closed_tabs: set[int] = set()
    closed_windows: set[int] = set()
    at = 8
    while at + 3 <= len(data):
        size, = struct.unpack_from("<H", data, at)
        at += 2
        if size == 0 or at + size > len(data):
            break
        command, body = data[at], data[at + 1:at + size]
        at += size
        try:
            if command == SET_TAB_WINDOW:
                window, tab = struct.unpack_from("<ii", body)
                tab_window[tab] = window
            elif command == SET_TAB_INDEX:
                tab, index = struct.unpack_from("<ii", body)
                tab_index[tab] = index
            elif command == UPDATE_NAVIGATION:
                tab, index = struct.unpack_from("<ii", body, 4)  # after the pickle's size
                url, where = _string(body, 12)
                title, _ = _string16(body, where)
                navs.setdefault(tab, {})[index] = (url, title)
            elif command == SELECTED_NAVIGATION:
                tab, index = struct.unpack_from("<ii", body)
                selected_nav[tab] = index
            elif command == SELECTED_TAB:
                window, index = struct.unpack_from("<ii", body)
                selected_tab[window] = index
            elif command == WINDOW_TYPE:
                window, kind = struct.unpack_from("<ii", body)
                kinds[window] = kind
            elif command == TAB_CLOSED:
                closed_tabs.add(struct.unpack_from("<i", body)[0])
            elif command == WINDOW_CLOSED:
                closed_windows.add(struct.unpack_from("<i", body)[0])
        except (struct.error, UnicodeDecodeError, ValueError):
            continue

    tabs: dict[int, list[tuple[int, str, str]]] = {}
    for tab, window in tab_window.items():
        if tab in closed_tabs or window in closed_windows or not navs.get(tab):
            continue
        if kinds.get(window, NORMAL_WINDOW) != NORMAL_WINDOW:
            continue
        entries = navs[tab]
        url, title = entries.get(selected_nav.get(tab, max(entries)), entries[max(entries)])
        tabs.setdefault(window, []).append((tab_index.get(tab, 0), url, title))
    windows = []
    for window, items in sorted(tabs.items()):
        items.sort(key=lambda item: item[0])
        chosen = selected_tab.get(window, 0)
        title = items[chosen][2] if 0 <= chosen < len(items) else ""
        windows.append(SessionWindow(window, profile, [url for _, url, _ in items], title))
    return windows


def _string(body: bytes, at: int) -> tuple[str, int]:
    n, = struct.unpack_from("<i", body, at)
    if n < 0 or at + 4 + n > len(body):
        raise ValueError("bad string")
    return body[at + 4:at + 4 + n].decode("utf-8", "replace"), at + 4 + ((n + 3) & ~3)


def _string16(body: bytes, at: int) -> tuple[str, int]:
    n, = struct.unpack_from("<i", body, at)
    if n < 0 or at + 4 + 2 * n > len(body):
        raise ValueError("bad string")
    return body[at + 4:at + 4 + 2 * n].decode("utf-16-le", "replace"), at + 4 + ((2 * n + 3) & ~3)


def profiles(browser: Browser, home: Path | None = None) -> list[str]:
    root = (home or config_home()) / browser.folder
    try:
        return sorted(p.name for p in root.iterdir() if PROFILE_RE.match(p.name) and (p / "Sessions").is_dir())
    except OSError:
        return []


def read_windows(browser: Browser, home: Path | None = None) -> list[SessionWindow]:
    """The open windows of every profile, from each one's newest session file."""
    root = (home or config_home()) / browser.folder
    windows = []
    for profile in profiles(browser, home):
        try:
            files = sorted((root / profile / "Sessions").glob("Session_*"), key=lambda p: p.stat().st_mtime)
            if not files or files[-1].stat().st_size > MAX_FILE:
                continue
            windows += parse(files[-1].read_bytes(), profile)
        except OSError:
            continue
    return windows


def pair(hypr_windows: list[dict], session_windows: list[SessionWindow], suffix: str) -> dict[str, SessionWindow]:
    """Hyprland window address -> its session window. `hypr_windows` are
    Hyprland clients of one browser."""
    def made(client: dict) -> int:
        try:
            return int(str(client.get("stableId") or ""), 16)
        except ValueError:
            return -1

    clients = sorted(hypr_windows, key=made)
    left = sorted(session_windows, key=lambda w: w.id)
    result: dict[str, SessionWindow] = {}

    def bare(client: dict) -> str:
        title = client.get("title") or ""
        return title[:-len(suffix)] if title.endswith(suffix) else title

    if len(clients) == len(left) and all(made(c) >= 0 for c in clients):
        return {c["address"]: w for c, w in zip(clients, left)}
    for client in clients:
        title = bare(client)
        same = [w for w in left if w.title and w.title == title]
        if len(same) == 1 and sum(1 for c in clients if bare(c) == title) == 1:
            result[client["address"]] = same[0]
    return result


def open_tabs(clients: list[dict], home: Path | None = None) -> dict[str, dict]:
    """Window address -> {"profile", "urls"} for every browser window whose
    tabs could be read. Only URLs Snapback would open again are kept, so a
    window showing just a New Tab page has none."""
    result = {}
    for app, browser in BROWSERS.items():
        mine = [c for c in clients if (c.get("initialClass") or c.get("class")) == app
                and c.get("mapped", True) and not c.get("pinned")]
        if not mine:
            continue
        for address, window in pair(mine, read_windows(browser, home), browser.suffix).items():
            result[address] = {"profile": window.profile, "urls": [u for u in window.urls if usable(u)]}
    return result


def running(browser: Browser, home: Path | None = None) -> bool:
    """Whether the browser is running for this user: its SingletonLock names
    a live process on this machine."""
    try:
        target = os.readlink((home or config_home()) / browser.folder / "SingletonLock")
        host, _, pid = target.rpartition("-")
        return host == os.uname().nodename and Path(f"/proc/{int(pid)}").exists()
    except (OSError, ValueError):
        return False


def restores_itself(browser: Browser, profile: str, home: Path | None = None) -> bool:
    """Whether the profile is set to continue where it left off."""
    try:
        prefs = json.loads(((home or config_home()) / browser.folder / profile / "Preferences").read_text())
        return (prefs.get("session") or {}).get("restore_on_startup") == 1
    except (OSError, ValueError, AttributeError):
        return False


def cold_restore(app: str, profile: str, home: Path | None = None) -> bool:
    """Whether opening a window of `app` now would bring back the browser's own
    last session instead: it isn't running, and is set to continue where it
    left off."""
    browser = BROWSERS.get(app)
    return bool(browser) and not running(browser, home) and restores_itself(browser, profile, home)


def new_window(exec_args: list[str], profile: str, urls: list[str]) -> list[str] | None:
    """The command that opens one window with these tabs, or None."""
    urls = [u for u in urls if usable(u)]
    if not exec_args or not urls:
        return None
    command = ["uwsm-app", "--", *exec_args]
    if profile != "Default" and PROFILE_RE.match(profile):
        command.append(f"--profile-directory={profile}")
    return command + ["--new-window", "--", *urls]
