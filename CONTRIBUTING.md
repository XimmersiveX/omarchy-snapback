# Contributing

Snapback is a small project with one maintainer. What helps most is hearing where it goes wrong on
desks that aren't mine: other screens, laptops, docks, apps and terminals.

## Report a bug

[Open an issue](https://github.com/XimmersiveX/omarchy-snapback/issues) and include:

- What you did, what you expected, and what happened instead.
- Your screens when you saved and when you restored or switched (a laptop, a dock, which monitors).
- The output of `python3 -m snapback doctor`, run from the plugin folder
  (`~/.config/omarchy/plugins/io.github.ximmersivex.snapback`), and your Omarchy version
  (`omarchy version`).
- Anything odd in `journalctl --user -u snapback`. Look it over first: it can hold window titles.

Search the open issues first. If yours is there, add what you saw to it instead of opening a new one.

## Ideas and features

Open an issue that describes the problem before writing code. Snapback stays small on purpose, so a
feature may be turned down even when it works well. Asking first saves your time.

## Pull requests

- **Fix one problem.** Include everything that fix needs (code, tests, README), and nothing else.
  Cleanups, refactors and a second fix go in their own pull requests.
- **Show that it works.** Say how you found the bug, how you checked the fix on a real desk, and what
  you saw. "Tests pass" isn't enough on its own. For anything you can see in the panel, add a before
  and after screenshot to the pull request. Don't commit them.
- **Run the tests:** `python3 -m unittest discover -s tests -t .` and `omarchy plugin validate .`
- **Standard library only.** Omarchy ships Python 3; Snapback adds no dependencies.
- **No private data.** Recordings of a real desk hold usernames, home paths, window titles and monitor
  serials. Make test fixtures with `tools/fixture.py`, which scrubs them.
- **Written with an agent?** That's fine. Say which one, and check its work yourself before you open
  the pull request. `AGENTS.md` has what an agent needs to know about this repo.

A pull request that follows all of this can still be closed. It isn't owed a merge, but it will get an
answer.

## License

By contributing, you agree that your work is released under the [MIT license](LICENSE).
