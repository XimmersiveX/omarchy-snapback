# Snapback

Save and restore your Omarchy windows and workspaces, so you can pick up exactly where you left off after a reboot or logout.

An Omarchy shell plugin. Status: early development.

Related requests: omacom/omarchy discussions #2123 and #472, and issues #8535 and #7085.

## The bar widget

This folder is the plugin: `manifest.json` at the top, the panel in `shell/`, and the
`snapback` command line beside it, which the panel runs with `python3` (no other
dependencies). The panel has a switch to restore your windows at login (it writes
Omarchy's post-boot hook and starts the autosave, or removes both), your saved
sessions (click one to put its windows back), "save open windows as", and how the
last restore went.

To try it from a checkout:

```bash
ln -sfn "$PWD" ~/.config/omarchy/plugins/snapback
omarchy-shell shell rescanPlugins
omarchy plugin enable io.github.ximmersivex.snapback
```

`omarchy-shell snapback toggle` opens it from a keybinding.

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

The panel's helpers run under `node` and its logic in an offscreen Quickshell; both
are skipped where those aren't installed. `omarchy plugin validate .` checks the manifest.

## License

MIT, see [LICENSE](LICENSE).
