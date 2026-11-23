# Snapback

Save and restore your Omarchy windows and workspaces, so you can pick up exactly where you left off after a reboot or logout.

An Omarchy shell plugin. Status: early development.

Related requests: omacom/omarchy discussions #2123 and #472, and issues #8535 and #7085.

## The bar widget

This folder is the plugin: `manifest.json` at the top, the panel in `shell/`, and the
`snapback` command line beside it, which the panel runs with `python3` (no other
dependencies). The panel has a switch to restore your windows at login (it writes
Omarchy's post-boot hook and starts the autosave, or removes both) and your saved
setups: one for coding, one for gaming, an empty one to start fresh. Click a setup
and the desk becomes it: its apps come back on their screens and workspaces, each
screen shows the workspace it showed, and windows the setup doesn't have close
(politely, so unsaved work can still ask). The desk as it was is kept as "Before
switch", so one click undoes a switch. "Save open windows as a setup" adds one.

To try it from a checkout:

```bash
ln -sfn "$PWD" ~/.config/omarchy/plugins/snapback
omarchy-shell shell rescanPlugins
omarchy plugin enable io.github.ximmersivex.snapback
```

`omarchy-shell snapback toggle` opens it and `omarchy-shell snapback switchTo NAME` switches
to a setup, e.g. from a keybinding.

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

The panel's helpers run under `node` and its logic in an offscreen Quickshell; both
are skipped where those aren't installed. `omarchy plugin validate .` checks the manifest.

## License

MIT, see [LICENSE](LICENSE).
