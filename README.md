# laop

A task-loop harness for Claude Code: an orchestrator session dispatches builder and reviewer
subagents task by task under a guard hook, with owner gates and a resolver that answers
non-decisional open questions on the owner's behalf. Plus a live viewer of the loops.

| Folder | What |
|---|---|
| [`skill/`](skill/) | The `/laop` Claude Code skill: the harness (`core/`), its installer (`tools/laop.py`: `init`, `adopt`, `upgrade`, `doctor`, `render`), templates and tests. Start from [`skill/SKILL.md`](skill/SKILL.md) and [`skill/core/harness/README.md`](skill/core/harness/README.md). |
| [`viewer/`](viewer/) | laop monitor: one browser page (or desktop window) for every repository that runs the loop. Standard library only. See [`viewer/README.md`](viewer/README.md). |
| [`assets/`](assets/) | Logo. |

## Install the skill

Link (or copy) `skill/` into your Claude Code skills directory as `laop`:

```
# Windows (junction)
mklink /J "%USERPROFILE%\.claude\skills\laop" "<path to this repo>\skill"
# macOS / Linux
ln -s "<path to this repo>/skill" ~/.claude/skills/laop
```

Then, in a repository, ask Claude Code for `/laop init`.

## Run the viewer

```
python viewer/viewer.py
```

## Tests

```
python -m unittest discover -s skill/tests
python -m unittest discover -s viewer/tests
```

Python 3.11+, standard library only (the desktop window needs `pywebview`, the build `pyinstaller` and `pillow`).

## Status

Experimental and changing quickly: the harness format and the installer's commands may change
between minor versions (see [`skill/CHANGELOG.md`](skill/CHANGELOG.md)). Use `/laop upgrade` to
move an installed repository to a new version.

## License

[MIT](LICENSE) © 2026 Luca Amerio
