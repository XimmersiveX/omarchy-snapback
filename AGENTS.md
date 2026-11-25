# Snapback

An Omarchy bar widget that saves the open windows as setups and switches between them. The panel
(`shell/`: Quickshell QML) is a thin layer over a Python command line (`snapback/`, standard library
only), which drives Hyprland through `hyprctl` and its `hl.*` Lua API and reads Omarchy's state files.

## Private notes

If `notes/` exists, read `notes/start-here.md` first. It is the maintainer's private hand-over and is
never committed (`notes` is in `.gitignore`).

## Run and test

- `python3 -m unittest discover -s tests -t .` (panel tests skip without node or quickshell)
- `omarchy plugin validate .`. It refuses any symlink in the folder, so with a `notes` link, validate
  a copy of the files git would publish: `d=$(mktemp -d); git ls-files -co --exclude-standard -z |
  tar -c --null -T - | tar -x -C "$d"; omarchy plugin validate "$d"`
- Run this checkout's code with `PYTHONPATH=$PWD python3 -P -m snapback ...`. An installed copy of the
  plugin may already be on `PYTHONPATH`, and then plain `python3 -m snapback` runs that one instead.
- `snapback restore --dry-run` plans a restore without touching anything.

## Ways to hurt yourself here

1. **The desk is someone's real desktop.** A live test opens, moves and closes real windows. Ask
   before taking over the screens; only touch windows of class `snapback-test-*` (`tools/scene.py`);
   run `tools/desk.py save` before and `check` after, and put every screen's view and focus back.
   Focus follows the mouse, so the user must not touch it during a test.
2. **The autosave watcher saves test windows too.** `systemctl --user stop snapback` before a live
   test, `python3 -m snapback setup` after. Never `systemctl --user restart snapback`: that reruns
   `login`, which is a full restore.
3. **Killing by pattern.** Never `pkill -f` a name or path: your own shell's command line matches.
4. **Private data in fixtures.** Raw captures hold usernames, home paths, window titles and monitor
   serials. `tools/fixture.py record` writes them to the git-ignored `notes/fixtures-raw/`; only
   `tools/fixture.py scrub` output goes in `tests/fixtures/`.
5. **Hyprland's Lua API moves.** Every Hyprland call goes through `snapback/hypr.py`, every Omarchy
   read through `snapback/omarchy.py`. After a Hyprland update, run `snapback doctor` and a real
   switch, then update `TESTED_HYPRLAND` in `snapback/__main__.py`.

## Before you call it done

A change must reach every place it applies:
- **CLI:** text output and `--json`.
- **Panel:** `shell/`. Omarchy's plugin hot reload keeps the old QML, so run `omarchy restart shell`
  before looking.
- **Watcher and login hook:** the running watcher keeps its old code until restarted
  (`systemctl --user stop snapback`, then `python3 -m snapback setup`); a watcher start rewrites an
  outdated login hook.
- **Old saved setups:** new fields are optional, and files saved before the change still restore.
- **Layouts and apps:** dwindle, scrolling, master and floating workspaces; Chromium, Chrome and
  Brave tabs; foot, Alacritty, Ghostty and Kitty.
- **README**, when what users see changes.

**Release rule:** commit, then `omarchy plugin update io.github.ximmersivex.snapback --yes` on the
desk, then see a live save in `journalctl --user -u snapback`, and only then push.

## Rules

- Standard library only; Omarchy ships Python 3.
- Never close or kill a user's window outside a switch's documented rules, and always turn
  `focus_on_activate` back on (restore does it in `finally`).
- Nothing private in the repo: no real names, emails, home paths, tokens. Author is XimmersiveX.
