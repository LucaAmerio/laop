# laop monitor

Live viewer of laop task loops: one browser page for every repository that runs the
loop, with an Overview tab (what runs where, what waits behind a question or a gate) and one tab
per repository (dependency graph, gates, the task in flight and its phase, time per task, blocked
tasks and why, questions to decide, provisional decisions, events, log, commits).

Separate from laop: nothing is installed in the repositories. It reads their `harness/` files
(state, tasks, log) and never writes them on its own. It changes a repository only when you ask:
a decision you apply from a discussion with Claude (below), what you ask Claude in a chat (below),
and the stop files: the "Soft stop", "Stop" and "Emergency stop" buttons create `AGENT_SOFT_STOP`
(the agent at work finishes), `AGENT_STOP` (the task agent commits and hands off) or
`AGENT_EMERGENCY_STOP` (every tool call blocked at once, work in progress lost) at the root of a
repository, and "Clear stops" removes them; "Stop" and "Emergency stop" ask for confirmation first.
The Overview has the same buttons for every watched repository at once. "Schedule…" creates one of
the three stop files at a date and time instead: the viewer keeps it in its list of repositories
and creates the file when the time comes (it must be running then; a stop missed while it was
closed is created when it starts again). The button then shows which stop and when; a click
changes or cancels it.

## Use

```
python viewer.py                      open http://127.0.0.1:8765 (next free port if busy)
python viewer.py repo=<path>          also watch this repository
python viewer.py browser=no           do not open the browser
python viewer.py port=8800            another port
python viewer.py config=<file>        another list of repositories
python viewer.py repo=<path> snapshot=<file>   one self-contained HTML file, then exit
```

Each repository has five views, on a row of their own with the stop buttons. **Chat** is Claude
Code in the repository, as in the desktop app (below). **Status** shows what runs now, the
progress, the events and what needs you: every gate, question and provisional decision explained
(what to check, the question, the resolver's recommendation, the challenger's verdict) with its
`owner_tools.py` command to copy. **Questions** opens the decisions and the questions with their
discussion (below). **Graph** is the dependency graph, left to right. **Logs** holds the tasks, the
loop log, the commits and the files. In a
window at least 900×600 the page does not scroll: the graph fits its box (wheel to zoom, drag to
move) and long lists scroll inside their card; a narrower window scrolls as one page.

**Questions** (in Status) lists every entry of the open questions file (`docs/open_questions.md`,
or `open_questions` of `harness/project.json`), open ones and provisional ones (to ratify) first,
decided ones too. A click shows
the whole entry and its decision; an open one can be discussed with Claude, who ends with the text
of the decision for you to write after `**Decision.**`. A decided one can be discussed too, to look
back at it: "Discuss with Claude" under its decision opens the discussion beside it (Hide closes
it); Claude explains what was decided and why, and nothing can be applied. The line between the
question and the discussion can be dragged to share the width (a double click goes back to the
default).

"Discuss with Claude" opens a discussion about one decision. Each turn runs `claude -p` in the
repository with your Claude Code sign-in (subscription, no API key; sign in once with `claude`
then `/login`), read-only: Read, Grep and Glob only, no MCP servers, no project settings (the
loop's hooks never see it). Claude first explains the problem, the question and the proposal in
plain words; when you reach a decision it proposes it, with an **Apply** button. After your
confirmation the viewer runs the proposed `owner_tools.py` command in the repository (a gate, a
question waiting for `decide`, a provisional decision), or writes the decision after
`**Decision.**` in the open questions file and marks the entry decided (changing nothing else in
the file). Before writing, it records the file as yours with `owner_tools.py owner_write`, since the
loop's guard never sees this server; after writing, it commits that file alone
(`owner: <id> decided (laop monitor)`). While the loop runs, a harness without `owner_write`
(older than 0.5.1) gets nothing written: upgrade it. A failed commit is reported: commit the file
yourself. Only the decision the server took from Claude's reply and
checked is applied, never text sent by the page. Going back to an item resumes its discussion.
Discussions live as long as the server.

**Chat** lists the repository's chats on the left (the ones of this run of the viewer, then the
earlier ones, and the decision discussions) above the plan usage: the 5-hour and weekly windows of
your subscription, also shown under Now in Status. The viewer reads them every minute while a page
is open, with Claude Code's own `/usage` (`claude -p /usage`: no model turn, no tokens; it starts
`claude` for a few seconds, so not more often), and with every answer of a chat. Each turn runs `claude -p` in the
repository and resumes the chat's session, with the model, effort and permission mode chosen under
the message box (they apply from the next turn). Unlike a discussion, a chat is a full Claude Code
session: the repository's `.claude/settings.json`, its permissions and the loop's hooks apply (no
MCP servers). Nobody can answer a permission prompt there, so a tool call the mode and the settings
do not allow is refused, and the chat says which. The bar in the chat's head is its context window.
A message sent while Claude works waits and goes with the next turn; Stop ends the turn.

**Start loop** sends `/laop-start` in the chat, which becomes the loop's orchestrator; the loop
runs until it finishes, halts or stops, its steps shown one per line. Claude can also propose
starting it (a START LOOP card with the same button), and asking it in words works the same: the
view follows `orchestrator_session` in `harness/state.json`, not the button. A message you write
in that chat takes the loop back, as in the desktop app. **Continue in desktop app** opens the
chat's session in the Claude desktop app (`claude --desktop --resume`, the same session id), for
its reply, fork and side chat; the chat here stops taking messages until **Take back here**. Chats
are kept in `~/.laop/chats` (one JSON file each) and come back after a restart.

Add a repository with the "+" tab (it asks for the path and lists the loops found nearby), remove
one with the x on its tab. The list is kept in `~/.laop/viewer.json`.

`python app.py` (same options but `port` and `browser`) shows the same page in a desktop window
(`pip install pywebview`). There the "Notify" button sends Windows toasts, because the window has
no browser notifications; they come from "laop monitor" with its icon (the app registers its
AppUserModelID `laop.monitor` under `HKCU\Software\Classes\AppUserModelId` at every start). A click
on a toast brings the window forward on the repository it is about, or starts it there: the toast
opens the link `laop-monitor:open?repo=<id>`, which the app registers for the current user
(`HKCU\Software\Classes\laop-monitor`) to run itself with `open=`; that one hands the repository to
the running window (its port is in `~/.laop/app.json`) and ends. In a browser, a click on a
notification does the same in the viewer's tab. The window keeps its settings in `~/.laop/webview`
and uses port 8775 when free, so the theme and the notification choice survive a restart; a second
window takes the next free port (on Windows two servers cannot share one).

After replacing `viewer.py`, restart it: the page warns when the running server is older than the
file.

## Files

- `viewer.py`: the server (standard library only). Listens on 127.0.0.1 and answers only its own
  page (Host and Origin checks; every change, chats included, accepts JSON only).
- `viewer.html`: the page, re-read at every request.
- `app.py`: the desktop window (pywebview) and its Windows toasts.
- `build.py`: `python build.py` makes the icon (`assets/laop.ico`, `assets/laop.png`, the "a" of the
  logo) and `dist/laop-monitor.exe`, one file with the page inside (`pip install pyinstaller pillow`).
- `tests/test_viewer.py`: `python -m unittest discover -s tests`; builds its test repositories with
  the laop skill's test helper, found at `../skill` or `LAOP_SKILL`.
