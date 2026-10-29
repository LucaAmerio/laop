# laop_view

Live viewer of laop task loops: one browser page for every repository that runs the
loop, with an Overview tab (what runs where, what waits behind a question or a gate) and one tab
per repository (dependency graph, gates, the task in flight and its phase, time per task, blocked
tasks and why, questions to decide, provisional decisions, events, log, commits).

Separate from laop: nothing is installed in the repositories. It reads their `harness/` files
(state, tasks, log) and never writes them on its own. It changes a repository only when you ask:
a decision you apply from a discussion with Claude (below), and the stop files: the "Soft stop", "Stop" and "Emergency stop" buttons create `AGENT_SOFT_STOP`
(the agent at work finishes), `AGENT_STOP` (the task agent commits and hands off) or
`AGENT_EMERGENCY_STOP` (every tool call blocked at once, work in progress lost) at the root of a
repository, and "Clear stops" removes them; "Stop" and "Emergency stop" ask for confirmation first.
The Overview has the same buttons for every watched repository at once.

## Use

```
python viewer.py                      open http://127.0.0.1:8765 (next free port if busy)
python viewer.py repo=<path>          also watch this repository
python viewer.py browser=no           do not open the browser
python viewer.py port=8800            another port
python viewer.py config=<file>        another list of repositories
python viewer.py repo=<path> snapshot=<file>   one self-contained HTML file, then exit
```

Each repository has two views. **Status** shows what runs now, the progress, the events and what
needs you: every gate, question and provisional decision explained (what to check, the question,
the resolver's recommendation, the challenger's verdict) with its `owner_tools.py` command to copy.
**Work** holds the dependency graph, the tasks, the loop log, the commits and the files. In a
window at least 900×600 the page does not scroll: the graph fits its box (wheel to zoom, drag to
move) and long lists scroll inside their card; a narrower window scrolls as one page.

**Questions** (in Status) lists every entry of the open questions file (`docs/open_questions.md`,
or `open_questions` of `harness/project.json`), open ones first, decided ones too. A click shows
the whole entry and its decision; an open one can be discussed with Claude, who ends with the text
of the decision for you to write after `**Decision.**`.

"Discuss with Claude" opens a discussion about one decision. Each turn runs `claude -p` in the
repository with your Claude Code sign-in (subscription, no API key; sign in once with `claude`
then `/login`), read-only: Read, Grep and Glob only, no MCP servers, no project settings (the
loop's hooks never see it). Claude first explains the problem, the question and the proposal in
plain words; when you reach a decision it proposes it, with an **Apply** button. After your
confirmation the viewer runs the proposed `owner_tools.py` command in the repository (a gate, a
question waiting for `decide`, a provisional decision), or writes the decision after
`**Decision.**` in the open questions file and marks the entry decided (changing nothing else in
the file, and committing nothing). Only the decision the server took from Claude's reply and
checked is applied, never text sent by the page. Going back to an item resumes its discussion.
Discussions live as long as the server.

Add a repository with the "+" tab (it asks for the path and lists the loops found nearby), remove
one with the x on its tab. The list is kept in `~/.laop/viewer.json`.

`python app.py` (same options but `port` and `browser`) shows the same page in a desktop window
(`pip install pywebview`). There the "Notify" button sends Windows toasts, because the window has
no browser notifications; they come from "laop view" with its icon (the app registers its
AppUserModelID `laop.view` under `HKCU\Software\Classes\AppUserModelId` at every start). The window keeps its settings in `~/.laop/webview` and uses port 8775
when free, so the theme and the notification choice survive a restart.

After replacing `viewer.py`, restart it: the page warns when the running server is older than the
file.

## Files

- `viewer.py`: the server (standard library only). Listens on 127.0.0.1 and answers only its own
  page (Host and Origin checks; changes to the list and the stop files accept JSON only).
- `viewer.html`: the page, re-read at every request.
- `app.py`: the desktop window (pywebview) and its Windows toasts.
- `build.py`: `python build.py` makes the icon (`assets/laop.ico`, `assets/laop.png`, the "a" of the
  logo) and `dist/laop-view.exe`, one file with the page inside (`pip install pyinstaller pillow`).
- `tests/test_viewer.py`: `python -m unittest discover -s tests`; builds its test repositories with
  the laop skill's test helper, found at `../skill` or `LAOP_SKILL`.
