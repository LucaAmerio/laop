"""laop_view: live viewer of laop task loops, for the owner.

A standalone tool, not part of the harness installed in each repository. Usage, in a terminal of
its own:

    python viewer.py                          serve the viewer on http://127.0.0.1:8765 and open it
    python viewer.py repo=<path>              also watch this repository (added to the list)
    python viewer.py port=8800                another port (the next free one is tried if busy)
    python viewer.py browser=no               do not open the browser
    python viewer.py config=<file>            another list of repositories (default ~/.laop/viewer.json)
    python viewer.py repo=<path> snapshot=<file>   write a self-contained HTML snapshot and exit

One viewer watches several repositories, one tab each, plus an overview tab: what runs in each, what
is blocked behind a question or a gate, and the progress bar. The page adds repositories ("+" tab,
asking for the path) and removes them; ``repo=`` adds one from the command line. The list is kept
in a file of the user (``~/.laop/viewer.json``).

The page (``viewer.html``, next to this script) polls the server every two seconds and shows, for each
repository, the tasks of the milestone, their dependencies and gates, the task in flight and its
phase, blocked tasks and why, provisional decisions, queued owner actions, events, the loop log and
the commits, and the time spent on each task.

Read-only except for the stop files: the only files of a repository the viewer writes are
AGENT_STOP and AGENT_SOFT_STOP at its root (the "Soft stop", "Stop now" and "Clear stops" buttons, per
repository or for all of them from the overview). Otherwise it writes only its list of repositories
and the snapshot you ask for, and takes no lock (state.json is written atomically, so a read never
sees half a file). Every other owner action is shown as the ``owner_tools.py`` command to copy.

Each gate, question and provisional decision that waits for the owner is shown explained (the
gate's description, the question, the resolver's recommendation, the challenger's verdict), with a
discussion with Claude: each turn runs ``claude -p`` in the repository with the owner's Claude Code
sign-in (no API key), read-only (Read, Grep and Glob only, no MCP servers, no project settings so
the loop's hooks never see it). Claude may end with a ``DECISION:`` line, shown as the command to
copy (or the decision text of an open questions entry) with an Apply button: on the owner's
confirmation the viewer runs that ``owner_tools.py`` command in the repository, or writes the
decision into the open questions file and marks the entry decided. Only the decision this server
extracted and checked from Claude's reply is applied, never text sent by the page. The server
listens on 127.0.0.1 only, answers only its own page
(Host and Origin checks), and serves a fixed list of harness files. The guard denies this script to
every loop role of laop 0.3.1 and earlier.

Standard library only; it does not import loop_config, so a broken project.json still shows.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import http.server
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from fnmatch import fnmatch
from pathlib import Path

HERE = Path(__file__).resolve().parent
PAGE = HERE / "viewer.html"
#: Child processes get no console window of their own (the packaged app has none to share).
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
#: The Python that runs a repository's owner_tools.py: in the packaged app sys.executable is the app.
PYTHON = (shutil.which("python") or shutil.which("py") or "python") if getattr(sys, "frozen", False) else sys.executable


def _own_digest() -> str:
    try:
        return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    except OSError:
        return ""


#: Digest of this script when the process started: the page warns when the file on disk differs
#: (an upgrade replaced it, the running server still serves the old data).
STARTED_DIGEST = _own_digest()
GATE = "owner-gate"
DEFAULT_PORT = 8765
MAX_FILE_BYTES = 1_000_000
LOG_TAIL_LINES = 400
COMMITS = 60
#: Stop files the page may create or remove at a repository root.
STOP_FILES = {"hard": "AGENT_STOP", "soft": "AGENT_SOFT_STOP"}

#: Files the page may open, relative to the repository (open questions and principles are added
#: from project.json).
FILE_PATTERNS = (
    "harness/verdicts/*.md", "harness/resolutions/*.md", "harness/*_prompt.md", "harness/STATUS.md",
    "harness/PROGRESS.md", "harness/NEXT_FINDINGS.md", "harness/claim.json", "harness/STEER*.md",
)


class Repo:
    """Reads the loop files of one repository; every read tolerates a missing or broken file."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.h = self.root / "harness"
        self._git_cache: tuple[str, list] = ("", [])
        self._times_cache: tuple[tuple, dict] = ((), {})
        self._snap_cache: tuple[str, dict | None] = ("", None)
        self._snap_lock = threading.Lock()

    # ------------------------------------------------------------------ helpers

    def read_json(self, rel: str, problems: list[str], default=None):
        path = self.root / rel
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            problems.append(f"{rel} unreadable: {exc}")
            return default

    def git(self, *args: str) -> str:
        try:
            out = subprocess.run(["git", *args], cwd=self.root, capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=10, creationflags=NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return out.stdout.strip() if out.returncode == 0 else ""

    def project(self) -> dict:
        data = self.read_json("harness/project.json", [], {}) or {}
        return data if isinstance(data, dict) else {}

    def file_patterns(self) -> list[str]:
        p = self.project()
        return [*FILE_PATTERNS, p.get("open_questions", "docs/open_questions.md"),
                p.get("principles_file", "docs/owner_principles.md")]

    def allowed_file(self, rel: str) -> Path | None:
        """The path of ``rel`` when the page may open it, else None."""
        rel = rel.replace("\\", "/").lstrip("/")
        if ".." in rel.split("/") or not any(fnmatch(rel, pat) for pat in self.file_patterns()):
            return None
        path = (self.root / rel).resolve()
        if self.root not in path.parents or not path.is_file():
            return None
        return path

    def read_file(self, rel: str) -> str | None:
        path = self.allowed_file(rel)
        if path is None:
            return None
        with open(path, "rb") as f:
            data = f.read(MAX_FILE_BYTES + 1)
        text = data[:MAX_FILE_BYTES].decode("utf-8-sig", errors="replace")
        return text + ("\n\n[... truncated by the viewer]" if len(data) > MAX_FILE_BYTES else "")

    def listed_files(self) -> list[dict]:
        """Verdicts, resolutions and the other viewable files that exist, newest first."""
        found = {}
        for pat in self.file_patterns():
            for path in self.root.glob(pat):
                if path.is_file():
                    rel = path.relative_to(self.root).as_posix()
                    found[rel] = {"path": rel, "mtime": path.stat().st_mtime}
        return sorted(found.values(), key=lambda f: -f["mtime"])

    @staticmethod
    def age(path: Path) -> float | None:
        """Seconds since ``path`` was modified, or None when it does not exist."""
        try:
            return max(0.0, time.time() - path.stat().st_mtime)
        except OSError:
            return None

    def watched(self) -> list[Path]:
        h = self.h
        return [h / "state.json", h / "tasks.json", h / "project.json", h / "roles.json", h / "step.lock",
                h / "owner_queue.json", h / "claim.json", h / "logs" / "guard_heartbeat.json",
                h / "logs" / "loop.log", h / "verdicts", h / "resolutions",
                self.root / "AGENT_STOP", self.root / "AGENT_SOFT_STOP", self.root / self.questions_rel(),
                self.root / ".git" / "logs" / "HEAD", self.root / ".git" / "HEAD"]

    def etag(self) -> str:
        parts = []
        for p in self.watched():
            try:
                s = p.stat()
                parts.append(f"{p.name}:{s.st_mtime_ns}:{s.st_size}")
            except OSError:
                parts.append(f"{p.name}:-")
        return hashlib.sha256("|".join(parts).encode()).hexdigest()[:20]

    def commits(self, owned: dict[str, str], owner: set[str]) -> list[dict]:
        key = self.git("rev-parse", "HEAD")
        if key and key == self._git_cache[0]:
            raw = self._git_cache[1]
        else:
            log = self.git("log", f"-{COMMITS}", "--format=%H%x1f%ct%x1f%an%x1f%s")
            raw = [line.split("\x1f") for line in log.splitlines() if line.count("\x1f") == 3]
            self._git_cache = (key, raw)
        return [{"sha": sha, "time": int(ct), "author": author, "subject": subject,
                 "task": owned.get(sha), "owner": sha in owner} for sha, ct, author, subject in raw]

    def times(self, current_task: str | None) -> dict[str, dict]:
        """task_times of the whole loop log, re-parsed only when the log changed."""
        path = self.h / "logs" / "loop.log"
        try:
            s = path.stat()
        except OSError:
            return {}
        key = (s.st_mtime_ns, s.st_size, current_task)
        if self._times_cache[0] != key:
            text = path.read_text(encoding="utf-8", errors="replace")
            self._times_cache = (key, task_times(text.splitlines(), current_task))
        return self._times_cache[1]

    def log_tail(self) -> list[str]:
        path = self.h / "logs" / "loop.log"
        try:
            with open(path, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 200_000))
                lines = f.read().decode("utf-8", errors="replace").splitlines()
        except OSError:
            return []
        return lines[-LOG_TAIL_LINES:]

    def name(self) -> str:
        return str(self.project().get("name") or self.root.name)

    def read_resolution(self, rel: str) -> dict:
        """The resolver's and the challenger's parts of a resolution file ({} when unreadable)."""
        text = self.read_file(rel or "") if rel else None
        return parse_resolution(text) if text else {}

    def questions_rel(self) -> str:
        return str(self.project().get("open_questions", "docs/open_questions.md"))

    def question_entries(self) -> list[dict]:
        """The entries of the open questions file, open and closed (docs/open_questions.md)."""
        text = self.read_file(self.questions_rel())
        return parse_questions(text) if text else []

    def decision(self, kind: str, item_id: str) -> dict | None:
        """The open decision ``kind``/``item_id`` of the current snapshot ("doc": an open entry
        of the open questions file), or None."""
        snap = self.snapshot()
        if kind == "doc":
            return next(({"kind": "doc", "file": snap["questions_file"], **q} for q in snap["questions_doc"]
                         if q["id"] == item_id and q["open"]), None)
        return next((d for d in snap["decisions"] if d["kind"] == kind and d["id"] == item_id), None)

    def set_stop(self, kind: str | None) -> None:
        """Create AGENT_STOP ("hard") or AGENT_SOFT_STOP ("soft") at the repository root, or
        remove both (None): the only files of a repository the viewer writes."""
        if kind is None:
            for name in STOP_FILES.values():
                (self.root / name).unlink(missing_ok=True)
            return
        stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        (self.root / STOP_FILES[kind]).write_text(f"Created by laop_view at {stamp}.\n", encoding="utf-8")

    # ------------------------------------------------------------------ snapshot

    def snapshot(self) -> dict:
        """The full data of the repository, recomputed only when a watched file changed."""
        tag = self.etag()
        with self._snap_lock:
            if self._snap_cache[0] != tag or self._snap_cache[1] is None:
                self._snap_cache = (tag, self._build_snapshot())
            return self._snap_cache[1]

    def summary(self) -> dict:
        """The part of the snapshot the overview shows (no log, commits, files or task texts)."""
        snap = self.snapshot()
        keep_st = ("status", "attempts", "block_type", "blocked_reason")
        tasks = [{"id": t["id"], "title": t.get("title", ""), "agent": t.get("agent"), "gate": t["gate"],
                  "final": t.get("final", False), "waiting_on": t["waiting_on"], "holds_up": t["holds_up"],
                  "time": t.get("time", {}),
                  "st": {k: (v[:400] if isinstance(v, str) else v) for k, v in t["st"].items() if k in keep_st}}
                 for t in snap["tasks"]]
        state = {**snap["state"], "events": snap["state"]["events"][-8:]}
        heavy = ("log", "commits", "files", "roles", "claim", "tasks", "state", "decisions", "questions_doc")
        return {k: v for k, v in snap.items() if k not in heavy} | {
            "name": snap["project"]["name"], "path": str(self.root), "tasks": tasks, "state": state}

    def _build_snapshot(self) -> dict:
        problems: list[str] = []
        project = self.project()
        lock = self.read_json("harness/core.lock.json", problems, {}) or {}
        doc = self.read_json("harness/tasks.json", problems, None)
        state = self.read_json("harness/state.json", problems, {}) or {}
        queue = self.read_json("harness/owner_queue.json", problems, {}) or {}
        heartbeat = self.read_json("harness/logs/guard_heartbeat.json", [], None)
        claim = self.read_json("harness/claim.json", [], None)
        roles = (self.read_json("harness/roles.json", problems, {}) or {}).get("roles", {})

        tasks_doc = doc.get("tasks", []) if isinstance(doc, dict) else []
        if doc is None:
            problems.append("harness/tasks.json not found: plan the milestone first.")
        st_tasks = state.get("tasks", {}) if isinstance(state.get("tasks"), dict) else {}
        tasks = build_tasks(tasks_doc, st_tasks, problems)
        cur_id = state["current"].get("task_id") if isinstance(state.get("current"), dict) else None
        times = self.times(cur_id)
        for t in tasks:
            t["time"] = times.get(t["id"], {})

        owned = {sha: tid for tid, st in st_tasks.items() for sha in st.get("own_commits", [])}
        owner_commits = set(state.get("owner_commits", []))
        current = state.get("current")
        if isinstance(current, dict):
            current = {k: v for k, v in current.items() if k not in ("integrity", "open_questions_hash")}
            if isinstance(current.get("spawn"), dict):
                current["spawn"] = {k: v for k, v in current["spawn"].items() if k != "token_line"}
        files = self.listed_files()
        decisions = decision_items(tasks, state, self.read_resolution)
        return {
            "generated": time.time(),
            "repo": str(self.root),
            "project": {
                "name": project.get("name") or self.root.name,
                "loop_command": project.get("loop_command"),
                "owner_language": project.get("owner_language"),
                "max_iterations": project.get("max_iterations", 100),
                "max_attempts_per_task": project.get("max_attempts_per_task", 3),
                "resolver": project.get("resolver", {}),
                "external_programs": project.get("external_programs", []),
                "version": lock.get("version"),
            },
            "milestone": doc.get("milestone") if isinstance(doc, dict) else None,
            "branch": self.git("rev-parse", "--abbrev-ref", "HEAD"),
            "head": self.git("rev-parse", "--short", "HEAD"),
            "tasks": tasks,
            "roles": {name: {"dispatch": bool(r.get("dispatch")), "reviewer": bool(r.get("reviewer"))}
                      for name, r in roles.items() if isinstance(r, dict)},
            "state": {
                "loop_active": bool(state.get("loop_active")),
                "halted": state.get("halted"),
                "current": current,
                "iterations": state.get("iterations", 0),
                "empty_runs": state.get("empty_runs", 0),
                "resolutions_this_run": state.get("resolutions_this_run", 0),
                "bound": bool(state.get("orchestrator_session")),
                "last_task": state.get("last_task"),
                "provisional": state.get("provisional", {}),
                "questions": state.get("questions", {}),
                "events": state.get("events", []),
                "owner_commits": sorted(owner_commits),
            },
            "decisions": decisions,
            "questions_doc": self.question_entries(),
            "questions_file": self.questions_rel(),
            "queue": queue.get("actions", []) if isinstance(queue, dict) else [],
            "claim": claim if isinstance(claim, dict) else None,
            "stops": {"hard": (self.root / "AGENT_STOP").exists(),
                      "soft": (self.root / "AGENT_SOFT_STOP").exists()},
            "step_lock_age": self.age(self.h / "step.lock"),
            "heartbeat": heartbeat if isinstance(heartbeat, dict) else None,
            "files": files,
            "log": self.log_tail(),
            "commits": self.commits(owned, owner_commits),
            "problems": problems,
            "server_stale": _own_digest() != STARTED_DIGEST,
        }


LOG_LINE = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\] (.*)$")
DISPATCH = re.compile(r"^dispatch (\S+) to ")
#: Log lines after which no task is being worked on (the loop is sequential).
IDLE_STARTS = ("loop finished", "loop stopped", "HALT:", "soft stop", "refused:")


def task_times(lines: list[str], current_task: str | None) -> dict[str, dict]:
    """Time spent on each task, from the whole loop log.

    The loop works on one task at a time: a task is being worked on from its ``dispatch`` line
    until the next dispatch, its own ``done`` or ``blocked`` line, or a line that ends the run
    (loop finished or stopped, halt). That covers the agent, the checks, the review and the
    resolver phases. ``work`` is the sum of those segments, in seconds; ``open_since`` is the
    start of the segment still running (the task in flight); ``first`` and ``done`` are the epoch
    times of the first dispatch and of the last done line; gates get ``ready`` (gate ready) and
    ``done``.
    """
    times: dict[str, dict] = {}
    seg: list | None = None  # [task id, start, last time the task was mentioned]

    def entry(tid: str) -> dict:
        return times.setdefault(tid, {"work": 0.0, "dispatches": 0})

    def close(at: float) -> None:
        nonlocal seg
        if seg:
            entry(seg[0])["work"] += max(0.0, at - seg[1])
            seg = None

    for line in lines:
        m = LOG_LINE.match(line)
        if not m:
            continue
        try:
            ts = time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            continue
        text = m.group(2)
        d = DISPATCH.match(text)
        if seg and (d or text.startswith(IDLE_STARTS) or text.startswith(
                (f"{seg[0]} done (", f"{seg[0]} blocked"))):
            close(ts)
        elif seg and seg[0] in text:
            seg[2] = ts
        if d:
            tid = d.group(1)
            e = entry(tid)
            e["dispatches"] += 1
            e.setdefault("first", ts)
            seg = [tid, ts, ts]
            continue
        done = re.match(r"^(\S+) done \(", text)
        if done:
            entry(done.group(1))["done"] = ts
        gate = re.match(r"^gate (\S+) ready for the owner", text)
        if gate:
            entry(gate.group(1)).setdefault("ready", ts)
    if seg:
        if seg[0] == current_task:
            entry(seg[0])["open_since"] = seg[1]
        else:  # the session ended without a closing line: count up to the last mention
            close(seg[2])
    return times


def build_tasks(tasks_doc: list, st_tasks: dict, problems: list[str]) -> list[dict]:
    """Tasks of tasks.json merged with their state, plus what the page derives from both."""
    ids = [t.get("id") for t in tasks_doc if isinstance(t, dict)]
    status = {tid: (st_tasks.get(tid) or {}).get("status", "todo") for tid in ids}
    dependents: dict[str, list[str]] = {tid: [] for tid in ids}
    for t in tasks_doc:
        for d in t.get("depends_on", []):
            if d in dependents:
                dependents[d].append(t["id"])
            else:
                problems.append(f"task {t['id']} depends on unknown task {d}")

    def downstream(tid: str) -> list[str]:
        seen: list[str] = []
        stack = list(dependents.get(tid, []))
        while stack:
            x = stack.pop()
            if x not in seen:
                seen.append(x)
                stack.extend(dependents.get(x, []))
        return [x for x in ids if x in seen]

    out = []
    for t in tasks_doc:
        if not isinstance(t, dict) or "id" not in t:
            continue
        st = dict(st_tasks.get(t["id"]) or {"status": "todo", "attempts": 0})
        waiting_on = [d for d in t.get("depends_on", []) if status.get(d) != "done"]
        gate = t.get("agent") == GATE
        s = st.get("status", "todo")
        held = [x for x in downstream(t["id"]) if status.get(x) != "done"] if s != "done" else []
        out.append({
            **t,
            "gate": gate,
            "dependents": dependents.get(t["id"], []),
            "waiting_on": waiting_on,
            "ready": not gate and s in ("todo", "in_progress") and not waiting_on,
            "holds_up": held,
            "st": st,
        })
    return out


RESOLUTION_LINE = re.compile(r"^\s*RESOLUTION:\s*(ANSWER|ESCALATE)\s*$")
CATEGORY_LINE = re.compile(r"^\s*Category:\s*(.*)$", re.I)
CHALLENGE_LINE = re.compile(r"^\s*CHALLENGE:\s*(AGREE|AMEND|COUNTER|ESCALATE)\s*$")
PART_CHARS = 6000


def parse_resolution(text: str) -> dict:
    """Split a resolution file (laop step_resolve): the resolver's part (its RESOLUTION and
    Category lines, then its recommendation) and the challenger's section, which starts at the
    first CHALLENGE line; the last CHALLENGE line is the challenger's verdict."""
    lines = text.splitlines()
    starts = [i for i, ln in enumerate(lines) if CHALLENGE_LINE.match(ln)]
    split = starts[0] if starts else len(lines)
    resolver, verdict, category = [], None, ""
    for ln in lines[:split]:
        m = RESOLUTION_LINE.match(ln)
        if m and verdict is None:
            verdict = m.group(1)
            continue
        c = CATEGORY_LINE.match(ln)
        if c and not category:
            category = c.group(1).strip()
            continue
        resolver.append(ln)
    out = {"resolver_verdict": verdict, "category": category,
           "resolver": "\n".join(resolver).strip()[:PART_CHARS]}
    if starts:
        out["challenge_verdict"] = CHALLENGE_LINE.match(lines[starts[-1]]).group(1)
        out["challenger"] = "\n".join(ln for ln in lines[split:] if not CHALLENGE_LINE.match(ln)).strip()[:PART_CHARS]
    return out


QUESTION_HEAD = re.compile(r"^##\s+(Q[\w.-]*)\s*[:.]\s*(.*?)\s*$")
QUESTION_FIELD = re.compile(r"^-\s*(Status|Raised by|Blocks):\s*(.*?)\s*$", re.I)
ENTRY_CHARS = 20000


def parse_questions(text: str) -> list[dict]:
    """The entries of the open questions file: ``## Qn: title``, then ``- Status:``, ``- Raised
    by:`` and ``- Blocks:`` lines and the text, its decision after ``**Decision.**``. An entry is
    open when its status says "open" (``open``, ``partly decided; item 2 open``)."""
    entries, cur = [], None
    for line in text.splitlines():
        m = QUESTION_HEAD.match(line)
        if m:
            cur = {"id": m.group(1), "title": m.group(2), "lines": []}
            entries.append(cur)
        elif line.startswith(("# ", "## ")):
            cur = None  # another section ends the entry
        elif cur is not None:
            cur["lines"].append(line)
    out = []
    for e in entries:
        fields, body = {}, []
        for line in e["lines"]:
            f = QUESTION_FIELD.match(line)
            if f and f.group(1).lower() not in fields:
                fields[f.group(1).lower()] = f.group(2)
            else:
                body.append(line)
        text = "\n".join(body).strip()
        decision = text.split("**Decision.**", 1)[1].strip() if "**Decision.**" in text else ""
        status = fields.get("status", "")
        out.append({"id": e["id"], "title": e["title"], "status": status,
                    "open": bool(re.search(r"\bopen\b", status, re.I)), "raised_by": fields.get("raised by", ""),
                    "blocks": fields.get("blocks", ""), "decision": decision[:ENTRY_CHARS], "body": text[:ENTRY_CHARS]})
    return out


def question_text(qid: str, blocked_reason: str) -> str:
    """The question as step_resolve wrote it in the block reason: '<qid>: <question> [<why>; ...'."""
    reason = blocked_reason or ""
    if reason.startswith(qid + ":"):
        reason = reason[len(qid) + 1:]
    return reason.split(" [", 1)[0].strip()


def decision_items(tasks: list[dict], state: dict, read_resolution) -> list[dict]:
    """What waits for an owner decision, explained: open gates, questions that wait with a
    recommendation, provisional decisions to ratify. ``read_resolution(path)`` parses a file."""
    by_id = {t["id"]: t for t in tasks}
    out = []
    for t in tasks:
        s = t["st"].get("status", "todo")
        if t["gate"] and s != "done" and (s == "awaiting_owner" or not t["waiting_on"]):
            out.append({"kind": "gate", "id": t["id"], "task": t["id"], "title": t.get("title", ""),
                        "description": t.get("description", ""), "final": bool(t.get("final")),
                        "depends_on": t.get("depends_on", []), "holds_up": t["holds_up"]})
    questions = state.get("questions") if isinstance(state.get("questions"), dict) else {}
    for qid, q in questions.items():
        if not isinstance(q, dict) or q.get("status") != "open":
            continue
        task = by_id.get(q.get("task"), {})
        out.append({"kind": "question", "id": qid, "task": q.get("task"), "title": task.get("title", ""),
                    "question": question_text(qid, task.get("st", {}).get("blocked_reason", "")),
                    "why": q.get("why", ""), "time": q.get("time"), "file": q.get("file", ""),
                    "holds_up": task.get("holds_up", []), **read_resolution(q.get("file", "")),
                    **({"category": q["category"]} if q.get("category") else {})})
    provisional = state.get("provisional") if isinstance(state.get("provisional"), dict) else {}
    for qid, p in provisional.items():
        if not isinstance(p, dict) or p.get("status") != "open":
            continue
        task = by_id.get(p.get("task"), {})
        out.append({"kind": "provisional", "id": qid, "task": p.get("task"), "title": task.get("title", ""),
                    "time": p.get("time"), "file": p.get("file", ""), **read_resolution(p.get("file", "")),
                    **({"category": p["category"]} if p.get("category") else {})})
    return out


def render_snapshot(repo: Repo) -> str:
    """The page with the data and every listed file embedded: one self-contained HTML file."""
    data = dict(repo.snapshot())  # a copy: the cached snapshot is shared with the server
    data["snapshot"] = True
    data["file_texts"] = {f["path"]: repo.read_file(f["path"]) for f in data["files"]}
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    page = PAGE.read_text(encoding="utf-8")
    marker = "<!--LAOP_SNAPSHOT-->"
    return page.replace(marker, f"<script>window.LAOP_SNAPSHOT = {blob};</script>", 1)


# --------------------------------------------------------------------------- several repositories


def is_harness_repo(path: Path) -> bool:
    """A repository with a task loop: harness/ holding tasks.json, state.json or project.json."""
    h = path / "harness"
    return h.is_dir() and any((h / n).is_file() for n in ("tasks.json", "state.json", "project.json"))


def repo_id(path: Path) -> str:
    return hashlib.sha256(str(path.resolve()).lower().encode()).hexdigest()[:10]


def default_config() -> Path:
    env = os.environ.get("LAOP_VIEWER_CONFIG")
    return Path(env) if env else Path.home() / ".laop" / "viewer.json"


class Registry:
    """The repositories the viewer watches, kept in a config file of the user (not of any
    repository), so that every viewer started from any repository shows the same tabs."""

    def __init__(self, config: Path):
        self.config = config
        self.lock = threading.Lock()
        self.repos: dict[str, Repo] = {}
        try:
            saved = json.loads(config.read_text(encoding="utf-8")).get("repos", [])
        except (OSError, ValueError, AttributeError):
            saved = []
        for entry in saved:
            path = Path(entry.get("path", "")) if isinstance(entry, dict) else None
            if path and str(path):
                self.repos.setdefault(repo_id(path), Repo(path))

    def save(self) -> None:
        self.config.parent.mkdir(parents=True, exist_ok=True)
        data = {"_doc": "Repositories shown by laop_view (viewer.py).",
                "repos": [{"path": str(r.root)} for r in self.repos.values()]}
        tmp = self.config.with_name(self.config.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.config)

    def add(self, raw: str) -> tuple[str | None, str]:
        """Add a repository by path; returns (id, "") or (None, the reason it was refused)."""
        raw = (raw or "").strip().strip('"').strip("'")
        if not raw:
            return None, "Give the path of a repository."
        path = Path(raw).expanduser()
        if not path.is_absolute():
            return None, f"Give an absolute path, not {raw!r}."
        if not path.is_dir():
            return None, f"{path} is not a directory."
        if not is_harness_repo(path):
            if path.name.lower() == "harness" and is_harness_repo(path.parent):
                path = path.parent  # the harness folder was given: its repository is meant
            else:
                return None, (f"No task loop in {path}: it needs harness/ with tasks.json, state.json "
                              "or project.json. Give the repository root.")
        rid = repo_id(path)
        with self.lock:
            if rid not in self.repos:
                self.repos[rid] = Repo(path)
                self.save()
        return rid, ""

    def remove(self, rid: str) -> bool:
        with self.lock:
            if self.repos.pop(rid, None) is None:
                return False
            self.save()
        return True

    def get(self, rid: str) -> Repo | None:
        return self.repos.get(rid)

    def listing(self) -> list[dict]:
        return [{"id": rid, "path": str(r.root), "name": r.name(), "ok": is_harness_repo(r.root)}
                for rid, r in list(self.repos.items())]

    def etag(self) -> str:
        parts = [f"{rid}:{r.etag()}" for rid, r in list(self.repos.items())]
        return hashlib.sha256("|".join(parts).encode()).hexdigest()[:20]

    def overview(self) -> dict:
        out = []
        for rid, r in list(self.repos.items()):
            if not is_harness_repo(r.root):
                out.append({"id": rid, "path": str(r.root), "name": r.root.name,
                            "missing": True, "problems": [f"{r.root} has no task loop (moved or deleted?)."]})
                continue
            out.append({"id": rid, **r.summary()})
        return {"generated": time.time(), "repos": out, "server_stale": _own_digest() != STARTED_DIGEST}

    def candidates(self) -> list[str]:
        """Repositories with a task loop next to the watched ones, not watched yet."""
        parents = {r.root.parent for r in self.repos.values()}
        roots = parents | {q.parent for q in parents} | {Path.home(), Path.home() / "Documents"}
        watched = {r.root.resolve() for r in self.repos.values()}
        found = []
        for root in sorted(roots):
            try:
                children = sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))
            except OSError:
                continue
            for p in children[:400]:
                if p.resolve() not in watched and is_harness_repo(p):
                    found.append(str(p))
        return found[:30]


# --------------------------------------------------------------------------- discussions with Claude


OT_CMD = "python harness/owner_tools.py"
CHAT_TOOLS = "Read,Grep,Glob"
CHAT_MAX_TEXT = 8000
CHAT_MAX_RUNNING = 3
CHAT_TURN_TIMEOUT = 600
#: The commands Claude may propose, per kind of decision; ``{id}`` is the item's id.
DECISION_FORMS = {
    "question": ('decide qid={id} verdict=agree|amend|differ [note="..."]',
                 re.compile(r'decide qid=(?P<id>\S+) verdict=(agree|amend|differ)(?: note="(?P<note>[^"]*)")?')),
    "provisional": ('ratify qid={id}  or  override qid={id} note="..."',
                    re.compile(r'(?:ratify qid=(?P<id>\S+)|override qid=(?P<id2>\S+) note="(?P<note>[^"]*)")')),
    "gate": ("pass_gate task={id}", re.compile(r"pass_gate task=(?P<id>\S+)")),
    # an open entry of the open questions file: the text of its decision, no command
    "doc": ("<the decision of {id}, one line>", None),
}
DECISION_LINE = re.compile(r"^\s*`?DECISION:\s*(.+?)`?\s*$", re.M)
CHAT_SYSTEM = """You help the owner of a laop task loop decide one item that waits for them in this \
repository: {what}. You can read the repository with Read, Grep and Glob and nothing else: you cannot \
change files or run commands, so never say you did. Reply in {language}.
The owner reads you to decide, often without the project's details in mind. Make each reply stand on \
its own: explain the problem in plain words (what the software does at that point, what is undecided \
or goes wrong, why it matters to the user of the software), then the question, then the options with \
their concrete consequences, then the proposal and why. Do not lean on identifiers the owner would \
have to look up: question and task ids, section numbers, other questions, documents, file, function or \
column names. When one of them matters, say in a few words what it is or does instead of only naming \
it. Be concise; say what you are unsure of. The owner decides, not you.
When the owner asks for the decision, or the discussion reaches one, end your reply with one line \
exactly in this form, and only one:
DECISION: {form}
{how}"""
HOW_COMMAND = ("It is the argument list of harness/owner_tools.py; the owner copies and runs it. A note is plain "
               "text without double quotes, backslashes, backticks or dollar signs.")
HOW_DOC = ("It is the text the owner writes after **Decision.** in the entry of {file}, in the style of the "
           "decided entries there; the owner copies it.")


def chat_intro(repo: Repo, item: dict) -> str:
    """The first message of a discussion: the item, where its texts are, what to do first."""
    snap = repo.snapshot()
    task = next((t for t in snap["tasks"] if t["id"] == item.get("task")), {})
    lines = []
    if item["kind"] == "doc":
        lines += [f"Open question {item['id']} of {item.get('file')}: {item.get('title', '')}",
                  f"Status: {item.get('status', '')}; blocks: {item.get('blocks') or 'nothing named'}.",
                  f"Read its whole entry in {item.get('file')} (the section '## {item['id']}:'), and the entries "
                  "it refers to."]
    elif item["kind"] == "gate":
        lines += [f"Owner gate {item['id']}: {item.get('title', '')}" + (" (final gate)" if item.get("final") else ""),
                  f"What the owner checks before passing it:\n{item.get('description') or '(no description)'}",
                  f"It depends on: {', '.join(item.get('depends_on', [])) or 'nothing'}; "
                  f"it holds up: {', '.join(item.get('holds_up', [])) or 'nothing'}."]
    else:
        what = "Question" if item["kind"] == "question" else "Provisional decision of the resolver"
        lines += [f"{what} {item['id']}, raised on task {item.get('task')} ({item.get('title', '')}).",
                  *([f"Question: {item['question']}"] if item.get("question") else []),
                  *([f"Why it reached the owner: {item['why']}"] if item.get("why") else []),
                  f"The resolver's recommendation and the challenger's section are in {item.get('file')}: read it."]
        if task:
            lines.append(f"Task {task['id']} description:\n{task.get('description') or '(none)'}")
            if task.get("spec_refs"):
                lines.append("Spec references: " + ", ".join(map(str, task["spec_refs"])))
    p = repo.project()
    lines.append(f"The owner's principles are in {p.get('principles_file', 'docs/owner_principles.md')}, the "
                 f"open questions in {p.get('open_questions', 'docs/open_questions.md')}, the loop's state in "
                 "harness/state.json and harness/tasks.json.")
    lines.append("Read what you need first. Then start with an explanation the owner can follow without "
                 "opening any file: the problem in context, the question, the options and their consequences, "
                 "and the proposal (the one already written down if you agree with it, else yours), as your "
                 "instructions describe. Then wait for the owner.")
    return "\n\n".join(lines)


def proposed_command(kind: str, item_id: str, text: str) -> str | None:
    """The owner_tools.py command of the last valid DECISION line of ``text`` for this item."""
    form = DECISION_FORMS.get(kind)
    found = None
    for raw in DECISION_LINE.findall(text):
        m = form[1].fullmatch(raw.strip()) if form else None
        if m and item_id in (m.groupdict().get("id"), m.groupdict().get("id2")):
            note = m.groupdict().get("note")
            if note is not None and re.search(r'["\\`$]', note):
                continue
            found = raw.strip()
    return f"{OT_CMD} {found}" if found else None


def claude_env() -> dict[str, str]:
    """The environment of a discussion: the owner's own Claude Code sign-in only. No API key or
    base URL, and none of the variables of a Claude session the viewer may have been started
    from (the desktop app, a terminal inside it); CLAUDE_CONFIG_DIR stays, it says where the
    sign-in is."""
    return {k: v for k, v in os.environ.items()
            if k == "CLAUDE_CONFIG_DIR" or not k.upper().startswith(("ANTHROPIC", "CLAUDE"))}


def claude_command() -> list[str] | None:
    """The command that runs Claude Code (tests replace it with a fake)."""
    exe = shutil.which("claude")
    return [exe] if exe else None


class Chat:
    """One discussion with Claude about one decision: each turn runs ``claude -p`` in the
    repository, read-only (Read, Grep, Glob; no MCP servers, no project settings, so the loop's
    hooks never see it), and resumes the previous turn's session."""

    def __init__(self, cid: str, repo: Repo, kind: str, item: dict):
        self.id, self.repo, self.kind, self.item_id = cid, repo, kind, item["id"]
        self.item = item
        self.intro = chat_intro(repo, item)
        self.session: str | None = None
        self.entries: list[dict] = []
        self.busy = False
        self.error = ""
        self.proc: subprocess.Popen | None = None
        self.lock = threading.Lock()
        self.created = time.time()

    def add(self, role: str, text: str, **extra) -> None:
        with self.lock:
            self.entries.append({"n": len(self.entries), "role": role, "text": text, **extra})

    def view(self, since: int) -> dict:
        with self.lock:
            return {"id": self.id, "kind": self.kind, "item": self.item_id, "busy": self.busy,
                    "error": self.error, "entries": self.entries[since:], "count": len(self.entries)}

    def system_prompt(self) -> str:
        what = {"gate": f"the owner gate {self.item_id}", "question": f"the question {self.item_id}",
                "provisional": f"the provisional decision {self.item_id}",
                "doc": f"the open question {self.item_id} of {self.item.get('file')}"}[self.kind]
        lang = self.repo.project().get("owner_language") or "the language the owner writes in"
        how = HOW_DOC.format(file=self.item.get("file")) if self.kind == "doc" else HOW_COMMAND
        return CHAT_SYSTEM.format(what=what, language=lang, how=how,
                                  form=DECISION_FORMS[self.kind][0].format(id=self.item_id))

    def send(self, text: str, shown: bool = True) -> str:
        """Start a turn in the background; "" or the reason it was refused. Until a turn has
        started a session (the first one failed: not signed in?), the introduction goes along."""
        exe = claude_command()
        if not exe:
            return "The claude command was not found: install Claude Code and sign in (claude, then /login)."
        with self.lock:
            if self.busy:
                return "Claude is still answering."
            self.busy, self.error = True, ""
        if shown:
            self.add("user", text)
            if self.session is None:
                text = f"{self.intro}\n\nThe owner says:\n{text}"
        threading.Thread(target=self._turn, args=(exe, text), daemon=True).start()
        return ""

    def stop(self) -> None:
        proc = self.proc
        if proc and proc.poll() is None:
            proc.kill()

    def apply(self, n: int) -> str:
        """Apply the decision Claude proposed in entry ``n``, on the owner's request: run its
        owner_tools.py command, or write the decision of an open questions entry. Only what this
        server extracted and checked from Claude's reply is applied, never text from the page.
        Returns "" or the reason it was refused; the outcome is added to the discussion."""
        with self.lock:
            e = self.entries[n] if 0 <= n < len(self.entries) else None
        if not e or not (e.get("command") or e.get("proposal")):
            return "That message has no decision to apply."
        if e.get("applied"):
            return "Already applied."
        if self.busy:
            return "Wait for Claude's answer first."
        if self.repo.decision(self.kind, self.item_id) is None:
            return f"{self.item_id} is not open any more: decided meanwhile?"
        if self.kind == "doc":
            ok, out = write_decision(self.repo, self.item_id, e["proposal"])
            if ok:  # the loop reads state.json, not the file: free the tasks held on this question
                for tid in tasks_blocked_on(self.repo, self.item_id):
                    ok2, out2 = run_owner_tool(self.repo, f"{OT_CMD} unblock task={tid}")
                    ok, out = ok and ok2, f"{out}\n{out2}"
        else:
            ok, out = run_owner_tool(self.repo, e["command"])
        if ok:
            e["applied"] = True
        self.add("applied" if ok else "apply-failed", out)
        return ""

    def _turn(self, exe: list[str], text: str) -> None:
        cmd = [*exe, "-p", "--output-format", "stream-json", "--verbose", "--tools", CHAT_TOOLS,
               "--allowedTools", CHAT_TOOLS, "--permission-mode", "default", "--strict-mcp-config",
               "--setting-sources", "user", "--append-system-prompt", self.system_prompt()]
        if self.session:
            cmd += ["--resume", self.session]
        timer = None
        try:
            self.proc = subprocess.Popen(cmd, cwd=self.repo.root, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                                         creationflags=NO_WINDOW, env=claude_env())
            timer = threading.Timer(CHAT_TURN_TIMEOUT, self.stop)
            timer.start()
            self.proc.stdin.write(text)
            self.proc.stdin.close()
            failed = ""
            for line in self.proc.stdout:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev.get("type") == "system" and ev.get("session_id"):
                    self.session = ev["session_id"]
                elif ev.get("type") == "assistant":
                    self._assistant(ev.get("message", {}).get("content") or [])
                elif ev.get("type") == "result" and ev.get("is_error"):
                    failed = str(ev.get("result") or ev.get("subtype") or "error")
            err = self.proc.stderr.read()
            code = self.proc.wait()
            if failed or code:
                self.error = explain_chat_error(failed or err.strip() or f"claude exited with code {code}")
        except OSError as exc:
            self.error = f"claude could not start: {exc}"
        finally:
            if timer:
                timer.cancel()
            if self.proc:
                for pipe in (self.proc.stdout, self.proc.stderr):
                    pipe.close()
            self.proc = None
            with self.lock:
                self.busy = False

    def _assistant(self, content: list) -> None:
        for block in content:
            if block.get("type") == "tool_use":
                args = block.get("input") or {}
                arg = str(args.get("file_path") or args.get("pattern") or args.get("path") or "")
                if Path(arg).is_absolute():
                    try:
                        arg = Path(arg).resolve().relative_to(self.repo.root).as_posix()
                    except (ValueError, OSError):
                        pass
                self.add("tool", f"{block.get('name', '?')} {arg}".strip())
            elif block.get("type") == "text" and block.get("text", "").strip():
                if block["text"].startswith("Failed to authenticate"):
                    continue  # reported once, as the turn's error
                body = DECISION_LINE.sub("", block["text"]).strip()
                if self.kind == "doc":  # the decision's text, to copy into the file: no command
                    found = DECISION_LINE.findall(block["text"])
                    extra = {"proposal": found[-1].strip()} if found else {}
                else:
                    cmd = proposed_command(self.kind, self.item_id, block["text"])
                    extra = {"command": cmd} if cmd else {}
                if body or extra:
                    self.add("assistant", body, **extra)


def run_owner_tool(repo: Repo, command: str) -> tuple[bool, str]:
    """Run a checked owner_tools.py command (``proposed_command``) in the repository, without a
    shell: the owner's own tool, which queues the action when the loop is running."""
    args = shlex.split(command[len(OT_CMD):])
    try:
        out = subprocess.run([PYTHON, "harness/owner_tools.py", *args], cwd=repo.root, capture_output=True,
                             text=True, encoding="utf-8", errors="replace", timeout=120, creationflags=NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"{command}\nnot run: {exc}"
    text = (out.stdout + out.stderr).strip()[:3000] or "(no output)"
    return out.returncode == 0, f"{command}\n{text}"


def tasks_blocked_on(repo: Repo, qid: str) -> list[str]:
    """The tasks the loop holds blocked on the question ``qid`` (block reason ``<qid>: ...``): the
    ones that need ``unblock`` once its entry in the open questions file is decided."""
    return [t["id"] for t in repo.snapshot()["tasks"]
            if t["st"].get("status") == "blocked" and t["st"].get("block_type") == "question"
            and str(t["st"].get("blocked_reason", "")).startswith(qid + ":")]


def write_decision(repo: Repo, qid: str, decision: str) -> tuple[bool, str]:
    """Write ``decision`` after **Decision.** in the entry ``qid`` of the open questions file and
    mark the entry decided; the rest of the file is untouched (line endings kept)."""
    rel = repo.questions_rel()
    path = repo.root / rel
    decision = " ".join(decision.split())
    try:
        lines = path.read_bytes().decode("utf-8-sig").splitlines(keepends=True)
    except OSError as exc:
        return False, f"{rel} unreadable: {exc}"
    start = next((i for i, ln in enumerate(lines)
                  if (m := QUESTION_HEAD.match(ln.rstrip("\r\n"))) and m.group(1) == qid), None)
    if start is None:
        return False, f"{qid} not found in {rel}."
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith(("# ", "## "))), len(lines))
    eol = "\r\n" if lines[start].endswith("\r\n") else "\n"
    body = lines[start + 1:end]
    while body and not body[-1].strip():  # keep the blank lines before the next entry
        body.pop()
    tail = lines[start + 1 + len(body):end]
    status = next((i for i, ln in enumerate(body) if re.match(r"^-\s*Status:", ln, re.I)), None)
    if status is not None:
        body[status] = f"- Status: decided{eol}"
    at = next((i for i, ln in enumerate(body) if "**Decision.**" in ln), None)
    new = f"**Decision.** {decision}{eol}"
    if at is None:
        body += [eol, new]
    else:  # the decision paragraph only: what follows it in the entry stays
        after = next((i for i in range(at + 1, len(body)) if not body[i].strip()), len(body))
        body = body[:at] + [new] + body[after:]
    text = "".join(lines[:start + 1] + body + (tail or [eol]) + lines[end:])
    tmp = path.with_name(path.name + ".laop_view.tmp")
    try:
        tmp.write_text(text, encoding="utf-8", newline="")
        os.replace(tmp, path)
    except OSError as exc:
        return False, f"{rel} not written: {exc}"
    return True, f"{rel}: {qid} marked decided, with the decision:\n{decision}"


def explain_chat_error(msg: str) -> str:
    if re.search(r"authenticat|log ?in|OAuth|credential", msg, re.I):
        return (f"{msg.strip()[:300]}\nSign in once in a terminal: run claude, then /login (the subscription "
                "account). The viewer uses that sign-in.")
    return msg.strip()[:600]


class Chats:
    """The discussions of this server, one per decision (a new one on request)."""

    def __init__(self):
        self.chats: dict[str, Chat] = {}
        self.lock = threading.Lock()

    def running(self) -> int:
        return sum(1 for c in self.chats.values() if c.busy)

    def start(self, repo: Repo, rid: str, kind: str, item_id: str, fresh: bool) -> tuple[Chat | None, str]:
        item = repo.decision(kind, item_id)
        if item is None:
            return None, "That decision is not open any more."
        cid = hashlib.sha256(f"{rid}|{kind}|{item_id}".encode()).hexdigest()[:16]
        with self.lock:
            chat = self.chats.get(cid)
            if chat and not fresh:
                return chat, ""
            if chat:
                chat.stop()
            if self.running() >= CHAT_MAX_RUNNING:
                return None, f"{CHAT_MAX_RUNNING} discussions are already waiting for Claude: wait for one."
            chat = self.chats[cid] = Chat(cid, repo, kind, item)
        why = chat.send(chat.intro, shown=False)
        if why:
            chat.error = why
        return chat, ""

    def get(self, cid: str) -> Chat | None:
        return self.chats.get(cid)

    def find(self, rid: str, kind: str, item_id: str) -> Chat | None:
        """The discussion already held about this item, if any (none is started)."""
        return self.chats.get(hashlib.sha256(f"{rid}|{kind}|{item_id}".encode()).hexdigest()[:16])


class Handler(http.server.BaseHTTPRequestHandler):
    registry: Registry
    chats: Chats = Chats()

    def log_message(self, fmt, *args):  # quiet: one terminal line per request is noise
        pass

    def send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def send_json(self, code: int, data, extra: dict | None = None) -> None:
        self.send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", extra)

    def local_origin(self) -> bool:
        """Requests must come from this page: a web site open in the same browser may not use the
        viewer (Host check against DNS rebinding, Origin check against cross-site requests)."""
        port = self.server.server_address[1]
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host", "") not in allowed:
            return False
        origin = self.headers.get("Origin")
        return origin is None or origin in {f"http://{a}" for a in allowed}

    def repo_from(self, query: dict) -> Repo | None:
        return self.registry.get((query.get("repo") or [""])[0])

    def do_GET(self):  # the http.server API names it
        url = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(url.query)
        try:
            if not self.local_origin():
                self.send(403, b"forbidden", "text/plain; charset=utf-8")
            elif url.path in ("/", "/index.html"):
                self.send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            elif url.path == "/api/overview":
                tag = self.registry.etag()
                if self.headers.get("If-None-Match") == tag:
                    self.send(304, b"", "application/json", {"ETag": tag})
                    return
                self.send_json(200, self.registry.overview(), {"ETag": tag})
            elif url.path == "/api/snapshot":
                repo = self.repo_from(query)
                if repo is None:
                    self.send_json(404, {"error": "unknown repository"})
                    return
                tag = repo.etag()
                if self.headers.get("If-None-Match") == tag:
                    self.send(304, b"", "application/json", {"ETag": tag})
                    return
                self.send_json(200, repo.snapshot(), {"ETag": tag})
            elif url.path == "/api/file":
                repo = self.repo_from(query)
                text = repo.read_file((query.get("path") or [""])[0]) if repo else None
                if text is None:
                    self.send(404, b"not a viewable file", "text/plain; charset=utf-8")
                else:
                    self.send(200, text.encode("utf-8"), "text/plain; charset=utf-8")
            elif url.path == "/api/candidates":
                self.send_json(200, {"candidates": self.registry.candidates()})
            elif url.path == "/api/chat/find":
                q = {k: (query.get(k) or [""])[0] for k in ("repo", "kind", "item")}
                chat = self.chats.find(q["repo"], q["kind"], q["item"])
                self.send_json(200 if chat else 404, chat.view(0) if chat else {"error": "no discussion yet"})
            elif url.path == "/api/chat":
                chat = self.chats.get((query.get("id") or [""])[0])
                if chat is None:
                    self.send_json(404, {"error": "unknown discussion"})
                    return
                try:
                    since = max(0, int((query.get("since") or ["0"])[0]))
                except ValueError:
                    since = 0
                self.send_json(200, chat.view(since))
            else:
                self.send(404, b"not found", "text/plain; charset=utf-8")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # the viewer must keep serving whatever one request hits
            self.send(500, f"viewer error: {exc}".encode(), "text/plain; charset=utf-8")

    def do_POST(self):  # the http.server API names it
        """Add or remove a watched repository, create or remove stop files: JSON only (a
        cross-site form cannot send it)."""
        url = urllib.parse.urlsplit(self.path)
        try:
            if not self.local_origin() or not self.headers.get("Content-Type", "").startswith("application/json"):
                self.send(403, b"forbidden", "text/plain; charset=utf-8")
                return
            length = min(int(self.headers.get("Content-Length") or 0), 40_000)
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            except ValueError:
                body = {}
            if url.path == "/api/repos/add":
                rid, why = self.registry.add(str(body.get("path", "")))
                self.send_json(200 if rid else 400, {"id": rid} if rid else {"error": why})
            elif url.path == "/api/repos/remove":
                ok = self.registry.remove(str(body.get("id", "")))
                self.send_json(200 if ok else 404, {"ok": ok})
            elif url.path == "/api/stop":
                # {"id": <repo id> or "all", "kind": "hard" | "soft" | null (remove both)}
                kind = body.get("kind")
                if kind is not None and kind not in STOP_FILES:
                    self.send_json(400, {"error": "kind must be hard, soft or null"})
                    return
                rid = str(body.get("id", ""))
                targets = ([r for r in list(self.registry.repos.values()) if is_harness_repo(r.root)]
                           if rid == "all" else [r for r in [self.registry.get(rid)] if r])
                if not targets:
                    self.send_json(404, {"error": "unknown repository"})
                    return
                failed = []
                for repo in targets:
                    try:
                        repo.set_stop(kind)
                    except OSError as exc:
                        failed.append(f"{repo.root}: {exc}")
                self.send_json(500 if failed else 200, {"ok": not failed, "count": len(targets), "failed": failed})
            elif url.path == "/api/chat/start":
                # {"repo": <id>, "kind": "gate" | "question" | "provisional", "id": <item id>, "new": bool}
                rid = str(body.get("repo", ""))
                repo = self.registry.get(rid)
                kind = str(body.get("kind", ""))
                if repo is None or kind not in DECISION_FORMS:
                    self.send_json(404, {"error": "unknown repository or decision"})
                    return
                chat, why = self.chats.start(repo, rid, kind, str(body.get("id", "")), bool(body.get("new")))
                self.send_json(200 if chat else 409, chat.view(0) if chat else {"error": why})
            elif url.path == "/api/chat/send":
                chat = self.chats.get(str(body.get("id", "")))
                text = str(body.get("text", "")).strip()
                if chat is None or not text:
                    self.send_json(404 if chat is None else 400, {"error": "unknown discussion" if chat is None else "empty message"})
                    return
                why = chat.send(text[:CHAT_MAX_TEXT])
                self.send_json(409 if why else 200, {"error": why} if why else chat.view(0))
            elif url.path == "/api/chat/apply":
                # {"id": <discussion>, "n": <entry with the proposed decision>}: the decision as this
                # server recorded it, never a command or text sent by the page
                chat = self.chats.get(str(body.get("id", "")))
                if chat is None:
                    self.send_json(404, {"error": "unknown discussion"})
                    return
                try:
                    n = int(body.get("n"))
                except (TypeError, ValueError):
                    n = -1
                why = chat.apply(n)
                self.send_json(409 if why else 200, {"error": why} if why else chat.view(0))
            elif url.path == "/api/chat/stop":
                chat = self.chats.get(str(body.get("id", "")))
                if chat:
                    chat.stop()
                self.send_json(200 if chat else 404, {"ok": bool(chat)})
            else:
                self.send(404, b"not found", "text/plain; charset=utf-8")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # the viewer must keep serving whatever one request hits
            self.send(500, f"viewer error: {exc}".encode(), "text/plain; charset=utf-8")


def serve(registry: Registry, port: int, open_browser: bool) -> int:
    handler = type("BoundHandler", (Handler,), {"registry": registry})
    server = None
    for p in range(port, port + 20):
        try:
            server = http.server.ThreadingHTTPServer(("127.0.0.1", p), handler)
            break
        except OSError:
            continue
    if server is None:
        print(f"No free port in {port}-{port + 19}.")
        return 1
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    names = ", ".join(r["name"] for r in registry.listing()) or "none yet: add one in the page"
    print(f"laop viewer\n  {url}\n  repositories: {names}\n  list kept in {registry.config}\n"
          "  read-only except the stop files; Ctrl+C to stop.")
    if open_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def main(argv: list[str]) -> int:
    opts: dict[str, str] = {}
    for arg in argv:
        key, sep, value = arg.partition("=")
        if not sep or key not in ("port", "browser", "snapshot", "repo", "config"):
            print(__doc__)
            return 2
        opts[key] = value
    start = Path(opts["repo"]).expanduser().resolve() if "repo" in opts else None
    if "snapshot" in opts:
        if start is None or not is_harness_repo(start):
            print("snapshot= needs repo=<path of a repository with a task loop>.")
            return 2
        repo = Repo(start)
        out = Path(opts["snapshot"] or f"laop_snapshot_{dt.datetime.now():%Y%m%d_%H%M%S}.html")
        out.write_text(render_snapshot(repo), encoding="utf-8")
        print(f"Snapshot written to {out.resolve()}")
        return 0
    try:
        port = int(opts.get("port", DEFAULT_PORT))
    except ValueError:
        print("port must be a number")
        return 2
    registry = Registry(Path(opts["config"]) if "config" in opts else default_config())
    if start is not None:
        rid, why = registry.add(str(start))
        if rid is None:
            print(why)
            return 2
    return serve(registry, port, opts.get("browser", "yes").lower() not in ("no", "0", "false"))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
