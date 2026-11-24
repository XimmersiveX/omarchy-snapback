"""Which app made each window, and how to start it again.

Adapter for freedesktop desktop entries and Linux's /proc. Snapback only ever
relaunches through a desktop entry installed on this machine, or a terminal
opened in a folder. It never replays a command line it saved, so a session
file can't be used to run anything that isn't already installed as an app.

How a window's app is found, first match wins:
    1. the user's config maps the window class to a desktop entry
    2. a desktop entry declares the class as its StartupWMClass
    3. a desktop entry is named after the class (foot -> foot.desktop)
    4. an Omarchy terminal app (omarchy-launch-tui btop gives the window class
       org.omarchy.btop) whose desktop entry runs in a terminal (btop.desktop,
       Terminal=true); it reopens in the default terminal under the same class
    5. the systemd unit the window's process runs in names a desktop entry
       (app-com.t3tools.T3Code-88383.scope -> com.t3tools.T3Code.desktop)
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class DesktopEntry:
    id: str                      # "chromium.desktop"
    name: str
    exec: str
    wm_class: str | None
    hidden_from_menus: bool      # NoDisplay=true: launchable, not listed
    terminal_emulator: bool      # Categories includes TerminalEmulator
    path: Path
    terminal: bool = False       # Terminal=true: the app runs inside a terminal


@dataclass(frozen=True)
class ProcInfo:
    """What /proc says about a window's process."""

    unit: str | None = None       # systemd unit from the cgroup path
    shell_cwd: str | None = None  # folder of a terminal's shell


@dataclass(frozen=True)
class Launch:
    how: str                 # "desktop", "terminal", or "tui" (an app run in a terminal)
    entry: str               # desktop entry id
    via: str                 # which rule found it: config, wm-class, entry-id, omarchy-tui, systemd-unit
    cwd: str | None = None   # terminals only
    app_id: str | None = None  # tui only: the window class to open it under

    def to_dict(self) -> dict:
        data = {"how": self.how, "entry": self.entry, "via": self.via}
        if self.cwd:
            data["cwd"] = self.cwd
        if self.app_id:
            data["app_id"] = self.app_id
        return data


@dataclass(frozen=True)
class Resolution:
    launch: Launch | None = None
    skip: str | None = None      # deliberately not restored, and why
    problem: str | None = None   # couldn't work out how to restore it


def data_dirs(env: dict | None = None) -> list[Path]:
    """XDG data dirs, most important first."""
    env = os.environ if env is None else env
    home = env.get("XDG_DATA_HOME") or str(Path(env.get("HOME", str(Path.home()))) / ".local/share")
    system = env.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    return [Path(home)] + [Path(d) for d in system.split(":") if d]


def parse_desktop_entry(text: str) -> dict[str, str]:
    """The [Desktop Entry] group's unlocalized keys."""
    values: dict[str, str] = {}
    group = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            group = line[1:-1]
            continue
        if group != "Desktop Entry" or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if "[" in key:  # Name[de]=...
            continue
        values.setdefault(key, value.strip())
    return values


def load_entries(dirs: list[Path]) -> dict[str, DesktopEntry]:
    """Installed apps by desktop entry id. An entry in an earlier dir hides the
    same id in later ones, including when it is Hidden=true (how a user removes
    a system app)."""
    entries: dict[str, DesktopEntry] = {}
    seen: set[str] = set()
    for base in dirs:
        apps = base / "applications"
        if not apps.is_dir():
            continue
        for path in sorted(apps.rglob("*.desktop")):
            entry_id = path.relative_to(apps).as_posix().replace("/", "-")
            if entry_id in seen:
                continue
            seen.add(entry_id)
            try:
                values = parse_desktop_entry(path.read_text(errors="replace"))
            except OSError:
                continue
            if values.get("Type", "Application") != "Application" or values.get("Hidden") == "true":
                continue
            if not values.get("Exec"):
                continue
            categories = values.get("Categories", "").split(";")
            entries[entry_id] = DesktopEntry(
                id=entry_id,
                name=values.get("Name", entry_id),
                exec=values["Exec"],
                wm_class=values.get("StartupWMClass") or None,
                hidden_from_menus=values.get("NoDisplay") == "true",
                terminal_emulator="TerminalEmulator" in categories,
                path=path,
                terminal=values.get("Terminal") == "true",
            )
    return entries


def read_proc(pid: int, root: Path = Path("/proc")) -> ProcInfo:
    """The systemd unit a process runs in and, for a terminal, its shell's folder.

    Missing or unreadable /proc entries give None rather than an error: the
    process may have exited since the snapshot.
    """
    if pid <= 0:
        return ProcInfo()
    proc = root / str(pid)
    unit = None
    try:
        for line in (proc / "cgroup").read_text().splitlines():
            if line.startswith("0::"):
                unit = line.rsplit("/", 1)[-1] or None
    except OSError:
        pass
    return ProcInfo(unit=unit, shell_cwd=_shell_cwd(proc, root))


def _shell_cwd(proc: Path, root: Path) -> str | None:
    # The same rule as Omarchy's omarchy-cmd-terminal-cwd: the newest child,
    # if it leads its own session on a terminal (a shell, not some helper).
    newest = 0
    try:
        for children in proc.glob("task/*/children"):
            for child in children.read_text().split():
                newest = max(newest, int(child))
    except (OSError, ValueError):
        return None
    if not newest:
        return None
    try:
        stat = (root / str(newest) / "stat").read_text()
        fields = stat.rsplit(")", 1)[1].split()
        session, tty = int(fields[3]), int(fields[4])
        if session != newest or tty == 0:
            return None
        return os.readlink(root / str(newest) / "cwd")
    except (OSError, ValueError, IndexError):
        return None


def ids_from_unit(unit: str | None) -> list[str]:
    """Candidate app ids in a systemd unit name, per the XDG app unit naming
    (app[-<launcher>]-<id>-<random>.scope, app[-<launcher>]-<id>@<random>.service)."""
    if not unit or not unit.startswith("app-"):
        return []
    body = unit[len("app-"):]
    if body.endswith(".scope"):
        body = body[: -len(".scope")]
        body = body.rsplit("-", 1)[0] if "-" in body else body
    elif body.endswith(".service"):
        body = body[: -len(".service")].split("@", 1)[0]
    else:
        return []
    body = re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), body)
    candidates = [body]
    if "-" in body:
        candidates.append(body.split("-", 1)[1])  # drop the launcher, e.g. "Hyprland-"
    return candidates


OMARCHY_TUI = "org.omarchy."
TUI_ID = re.compile(r"^[A-Za-z0-9._-]+$")


def exec_args(exec_line: str) -> list[str]:
    """A desktop entry's Exec line as arguments, without field codes (%U, %f...).
    None of them when the line can't be read (an unclosed quote)."""
    import shlex
    try:
        words = shlex.split(exec_line)
    except ValueError:
        return []
    args = []
    for arg in words:
        if re.fullmatch(r"%[fFuUdDnNickvm]", arg):
            continue
        args.append(arg.replace("%%", "%"))
    return args


@dataclass
class Resolver:
    entries: dict[str, DesktopEntry]
    mapping: dict[str, str] = field(default_factory=dict)        # window class -> entry id
    never_restore: list[str] = field(default_factory=list)       # case-insensitive substrings

    def resolve(self, app_class: str, proc: ProcInfo | None = None) -> Resolution:
        proc = proc or ProcInfo()
        if not app_class:
            return Resolution(skip="window has no app class")
        if self.is_never(app_class):
            return Resolution(skip=f"never restore: {app_class}")

        entry, via = self._find(app_class, proc)
        if entry is None:
            if via:  # config named an entry that isn't installed
                return Resolution(problem=via)
            return Resolution(problem=f"no installed app matches window class {app_class!r}; "
                                      f"map it under [apps] in Snapback's config")
        if self.is_never(entry.id) or self.is_never(entry.name):
            return Resolution(skip=f"never restore: {entry.name}")
        if via == "omarchy-tui":
            return Resolution(launch=Launch("tui", entry.id, via, app_id=app_class))
        if entry.terminal_emulator:
            return Resolution(launch=Launch("terminal", entry.id, via, cwd=proc.shell_cwd))
        return Resolution(launch=Launch("desktop", entry.id, via))

    def is_never(self, text: str) -> bool:
        low = text.casefold()
        return any(pattern.casefold() in low for pattern in self.never_restore if pattern)

    def _find(self, app_class: str, proc: ProcInfo) -> tuple[DesktopEntry | None, str]:
        if app_class in self.mapping:
            wanted = self.mapping[app_class]
            entry = self.entries.get(wanted) or self.entries.get(wanted + ".desktop")
            if entry:
                return entry, "config"
            return None, f"config maps {app_class!r} to {wanted!r}, which isn't installed"

        for match in (lambda e: e.wm_class == app_class,
                      lambda e: e.wm_class is not None and e.wm_class.casefold() == app_class.casefold()):
            found = [e for e in self.entries.values() if match(e)]
            if found:
                # A listed app beats a hidden helper entry for the same window.
                found.sort(key=lambda e: e.hidden_from_menus)
                return found[0], "wm-class"

        entry = self._by_id(app_class)
        if entry:
            return entry, "entry-id"

        if app_class.startswith(OMARCHY_TUI) and TUI_ID.match(app_class):
            entry = self._by_id(app_class[len(OMARCHY_TUI):])
            if entry and entry.terminal:
                return entry, "omarchy-tui"

        for candidate in ids_from_unit(proc.unit):
            entry = self._by_id(candidate)
            if entry:
                return entry, "systemd-unit"
        return None, ""

    def _by_id(self, name: str) -> DesktopEntry | None:
        exact = self.entries.get(name + ".desktop")
        if exact:
            return exact
        low = name.casefold() + ".desktop"
        for entry_id, entry in self.entries.items():
            if entry_id.casefold() == low:
                return entry
        return None


def default_terminal() -> str | None:
    """The desktop entry xdg-terminal-exec opens, e.g. "foot.desktop"."""
    import subprocess
    try:
        result = subprocess.run(["xdg-terminal-exec", "--print-id"],
                                capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    line = result.stdout.strip().splitlines()[:1]
    return line[0].split(":", 1)[0] if result.returncode == 0 and line else None
