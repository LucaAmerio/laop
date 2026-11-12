"""Deterministic core of the task loop (core file: do not edit).

The loop runs inside an interactive Claude Code session. The main conversation acts as a thin
orchestrator: it runs the step scripts and spawns the subagents they ask for. Every decision
(which task, whether it passed, whether to retry, resolve, block or halt) is taken here, in code:

- ``step_next.py``: picks the next task and writes the subagent prompt;
- ``step_verify.py``: after the subagent, checks protected paths, integrity, the claim, runs the
  acceptance and global checks; asks for a review, or for a resolution of the agent's question;
- ``step_record.py``: after the reviewer, records its verdict;
- ``step_resolve.py``: after the resolver and the challenger, records the resolution.

Standard library only: the scripts must run with any Python on PATH. Checks run with the
interpreters of ``harness/project.json``.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import secrets
import subprocess
import time
import urllib.request
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from pathlib import Path

import approvals
import loop_config as C
import pathrules
import statefile

REPO = C.REPO
HARNESS = REPO / "harness"
TASKS_FILE = HARNESS / "tasks.json"
STATE_FILE = HARNESS / "state.json"
CLAIM_FILE = HARNESS / "claim.json"
#: One findings file per task: a section per rejected attempt, appended, never overwritten.
FINDINGS_DIR = HARNESS / "findings"
#: The single findings file of laop <= 0.4.0: read only for a task that has no findings file.
LEGACY_FINDINGS_FILE = HARNESS / "NEXT_FINDINGS.md"
#: Last turn that ended on an API error (StopFailure: usage limit, overload), by notify_hook.py.
STOP_FAILURE_FILE = HARNESS / "logs" / "stop_failure.json"
STEER_FILE = HARNESS / "STEER.md"
STATUS_FILE = HARNESS / "STATUS.md"
PROMPT_FILE = HARNESS / "current_prompt.md"
REVIEW_PROMPT_FILE = HARNESS / "review_prompt.md"
RESOLVE_PROMPT_FILE = HARNESS / "resolve_prompt.md"
CHALLENGE_PROMPT_FILE = HARNESS / "challenge_prompt.md"
VERDICT_DIR = HARNESS / "verdicts"
RESOLUTION_DIR = HARNESS / "resolutions"
LOG_DIR = HARNESS / "logs"
QUEUE_FILE = HARNESS / "owner_queue.json"
STEP_LOCK = HARNESS / "step"  # lock file harness/step.lock
#: The owner's three stops, from the most patient: soft (after the agent at work), stop (the task
#: agent commits and hands off, the guard tells it to) and emergency (the guard blocks everything).
SOFT_STOP_FILE = REPO / "AGENT_SOFT_STOP"
STOP_FILE = REPO / "AGENT_STOP"
EMERGENCY_STOP_FILE = REPO / "AGENT_EMERGENCY_STOP"
#: Start of every guard denial caused by AGENT_STOP (guard.py STOP_PREFIX).
STOP_DENIAL = "AGENT_STOP is present"
OPEN_QUESTIONS = REPO / C.OPEN_QUESTIONS_REL
PRINCIPLES = REPO / C.PRINCIPLES_REL
HEARTBEAT = LOG_DIR / "guard_heartbeat.json"
HEARTBEAT_MAX_AGE_S = 120
#: Events kept in harness/state.json (the full history stays in the loop log).
MAX_EVENTS = 10000

#: Pseudo-agent of a gate task: never dispatched; the owner passes it (owner_tools, pass_gate).
GATE = "owner-gate"
ORCHESTRATOR = pathrules.ORCHESTRATOR_ROLE
RESOLVER, CHALLENGER = pathrules.RESOLVER_ROLES
TERMINAL_WORDS = ("LOOP FINISHED", "HALTED", "STOPPED", "REFUSED")
BLOCK_TYPES = ("question", "owner_action", "harness")


class LoopError(Exception):
    """Configuration problem the owner must fix (bad tasks.json, git failure...)."""


# --------------------------------------------------------------------------- utilities


def now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(message: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_DIR / "loop.log", "a", encoding="utf-8") as f:
        f.write(f"[{now()}] {message}\n")


def load_json(path: Path, default=None):
    if not path.exists():
        return default
    with open(path, encoding="utf-8-sig") as f:
        return json.load(f)


def save_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def git(*args: str, strip: bool = True) -> str:
    proc = subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, encoding="utf-8")
    if proc.returncode != 0:
        raise LoopError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip() if strip else proc.stdout


def head_sha() -> str:
    return git("rev-parse", "HEAD")


def current_branch() -> str:
    return git("rev-parse", "--abbrev-ref", "HEAD")


def dirty_files() -> list[str]:
    """Uncommitted tracked or untracked files (git-ignored files excluded)."""
    files: list[str] = []
    for line in git("status", "--porcelain", "--untracked-files=all", strip=False).splitlines():
        path = line[3:]
        files += path.split(" -> ") if " -> " in path else [path]
    return [f.strip('"') for f in files if f]


def untracked_files() -> list[str]:
    """Untracked files that git does not ignore (never committed, never staged)."""
    lines = git("status", "--porcelain", "--untracked-files=all", strip=False).splitlines()
    return [line[3:].strip('"') for line in lines if line.startswith("?? ")]


def quarantine(paths: list[str], label: str) -> Path:
    """Move untracked files out of the working tree, into harness/logs/quarantine/<label>/.

    Used for files a subagent left behind without committing them (scratch output, the raw file
    of a failed run): kept for the owner, never deleted; emptied directories are removed.
    """
    dest_root = LOG_DIR / "quarantine" / label
    for rel in paths:
        src = REPO / rel
        dest = dest_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(src, dest)
        parent = src.parent
        while parent != REPO and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent
    return dest_root


def changed_since(base: str, excluded: list[str] | tuple[str, ...] = ()) -> list[str]:
    """Files changed by the commits in base..HEAD (except ``excluded``) and in the working tree."""
    if not excluded:
        committed = git("diff", "--name-only", f"{base}..HEAD").splitlines()
    else:
        skip = {git("rev-parse", sha) for sha in excluded}
        committed = []
        for sha in git("rev-list", f"{base}..HEAD").splitlines():
            if sha not in skip:
                committed += git("diff-tree", "--no-commit-id", "--name-only", "-r", "--root",
                                 sha).splitlines()
    return sorted(set(committed) | set(dirty_files()))


def record_own_commits(state: dict, task_id: str, dispatch_sha: str) -> None:
    """Record as the task's own the commits made since its current dispatch (one task runs at a
    time, so every commit in dispatch_sha..HEAD is the dispatched task's)."""
    own = state["tasks"][task_id].setdefault("own_commits", [])
    for sha in git("rev-list", "--reverse", f"{dispatch_sha}..HEAD").splitlines():
        if sha not in own:
            own.append(sha)


def excluded_commits(state: dict, task_id: str, base: str | None = None) -> list[str]:
    """Commits in the task's range not attributed to ``task_id``: owner commits, commits recorded
    as foreign to it, and, for a tracked task, every commit of base..HEAD not its own."""
    st = state["tasks"].get(task_id, {})
    extra = list(st.get("foreign_commits", []))
    if base and st.get("tracked"):
        own = set(st.get("own_commits", []))
        extra += [sha for sha in git("rev-list", "--reverse", f"{base}..HEAD").splitlines()
                  if sha not in own]
    result = list(state.get("owner_commits", []))
    for sha in extra:
        if sha not in result:
            result.append(sha)
    return result


def stop_failure_since(t: float) -> str:
    """The error of a StopFailure recorded at or after time ``t`` (notify_hook.py), else ''."""
    data = load_json(STOP_FAILURE_FILE) if STOP_FAILURE_FILE.exists() else None
    if isinstance(data, dict) and isinstance(data.get("time"), (int, float)) and data["time"] >= t:
        return str(data.get("error") or "StopFailure")
    return ""


def commits_since(base: str, excluded: list[str] | tuple[str, ...] = ()) -> int:
    skip = {git("rev-parse", sha) for sha in excluded}
    return sum(1 for sha in git("rev-list", f"{base}..HEAD").splitlines() if sha not in skip)


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""


def tail(text: str, n_lines: int = 60) -> str:
    return "\n".join(text.splitlines()[-n_lines:])


VERBOSITY_RANK = {"off": 0, "important": 1, "all": 2}


def notify(title: str, body: str, level: str = "important") -> None:
    """Log an event and push it to ntfy when ``level`` is within the configured verbosity.

    ``level`` is "important" (the owner must act or know) or "all" (progress information).
    The environment variable LAOP_NTFY_OFF (set by the tests) disables the push.
    """
    title = f"{C.NAME}: {title}"
    log(f"NOTIFY [{level}] {title}: {body[:300]}")
    configured = VERBOSITY_RANK.get(C.NTFY_VERBOSITY, 1)
    if os.environ.get("LAOP_NTFY_OFF") or not C.NTFY_TOPIC or VERBOSITY_RANK.get(level, 1) > configured:
        return
    try:
        req = urllib.request.Request(f"https://ntfy.sh/{C.NTFY_TOPIC}", data=body[:3500].encode("utf-8"),
                                     headers={"Title": title[:200]}, method="POST")
        urllib.request.urlopen(req, timeout=15)
    except Exception as exc:  # a failed notification must never break the loop
        log(f"notification failed: {exc}")


def new_token() -> str:
    return secrets.token_hex(4)


# --------------------------------------------------------------------------- tasks and state


def dispatch_roles() -> set[str]:
    return pathrules.dispatch_roles(REPO)


def reviewer_roles() -> set[str]:
    return pathrules.reviewer_roles(REPO)


def load_tasks() -> dict:
    doc = load_json(TASKS_FILE)
    if doc is None:
        raise LoopError("harness/tasks.json not found. Plan the milestone first.")
    agents, reviewers = dispatch_roles(), reviewer_roles()
    ids = [t["id"] for t in doc["tasks"]]
    if len(ids) != len(set(ids)):
        raise LoopError("Duplicate task ids in harness/tasks.json.")
    for t in doc["tasks"]:
        if t.get("agent") not in agents | {GATE}:
            raise LoopError(f"Task {t['id']}: agent must be one of {sorted(agents | {GATE})} "
                            "(roles with \"dispatch\": true in harness/roles.json).")
        for dep in t.get("depends_on", []):
            if dep not in ids:
                raise LoopError(f"Task {t['id']} depends on unknown task {dep}.")
        if t["agent"] == GATE:
            continue
        if not t.get("acceptance"):
            raise LoopError(f"Task {t['id']} has no acceptance checks (default FAIL needs some).")
        if t.get("review", True) and t.get("reviewer", "evaluator") not in reviewers:
            raise LoopError(f"Task {t['id']}: reviewer must be one of {sorted(reviewers)} "
                            "(roles with \"reviewer\": true in harness/roles.json).")
    return doc


def load_state(doc: dict) -> dict:
    state = statefile.read(STATE_FILE)
    state.setdefault("tasks", {})
    state.setdefault("events", [])
    state.setdefault("iterations", 0)
    state.setdefault("empty_runs", 0)
    state.setdefault("loop_active", False)
    state.setdefault("current", None)
    state.setdefault("halted", None)
    state.setdefault("provisional", {})
    state.setdefault("questions", {})
    state.setdefault("ledger", {})
    state.setdefault("resolutions_this_run", 0)
    for t in doc["tasks"]:
        state["tasks"].setdefault(t["id"], {"status": "todo", "attempts": 0})
    return state


def save_state(doc: dict, state: dict) -> None:
    statefile.save_owned(STATE_FILE, state)
    write_status(doc, state)


def state_digest(state: dict) -> str:
    """Digest of the part of the state the step scripts own (integrity check, F4)."""
    owned = {k: v for k, v in state.items() if k not in statefile.EXTERNAL_KEYS}
    if isinstance(owned.get("current"), dict):
        owned["current"] = {k: v for k, v in owned["current"].items() if k != "integrity"}
    return hashlib.sha256(json.dumps(owned, sort_keys=True).encode()).hexdigest()


@contextmanager
def step():
    """Run a step script: hold the step lock, load tasks and state, apply queued owner actions.

    Yields ``(doc, state, digest)``; step_verify compares ``digest`` with the one taken at dispatch.
    Owner actions are authorised changes: when the state was intact before they were applied, the
    dispatch baseline moves with them; when it was not, the baseline stays and the change is caught.
    """
    try:
        with statefile.locked(STEP_LOCK, timeout=30, stale=3600):
            doc = load_tasks()
            state = load_state(doc)
            digest = state_digest(state)
            if drain_owner_queue(doc, state):
                integrity = (state.get("current") or {}).get("integrity")
                if integrity and integrity.get("harness/state.json") == digest:
                    digest = state_digest(state)
                    integrity["harness/state.json"] = digest
                save_state(doc, state)
            yield doc, state, digest
    except statefile.LockBusy:
        raise LoopError("another step script or owner tool is running (harness/step.lock); "
                        "wait for it, or delete the lock file if no harness process runs") from None


def event(state: dict, text: str) -> None:
    state["events"] = (state["events"] + [f"{now()}  {text}"])[-MAX_EVENTS:]
    log(text)


def task_by_id(doc: dict, task_id: str) -> dict:
    return next(t for t in doc["tasks"] if t["id"] == task_id)


def _deps_done(t: dict, state: dict) -> bool:
    return all(state["tasks"][d]["status"] == "done" for d in t.get("depends_on", []))


def next_task(doc: dict, state: dict) -> dict | None:
    """First task, in file order, that can be dispatched now (gates are never dispatched)."""
    for t in doc["tasks"]:
        st = state["tasks"][t["id"]]
        if t["agent"] != GATE and st["status"] in ("todo", "in_progress") and _deps_done(t, state):
            return t
    return None


def open_gates(doc: dict, state: dict) -> list[dict]:
    """Gates whose dependencies are done and that the owner has not passed yet."""
    gates = []
    for t in doc["tasks"]:
        st = state["tasks"][t["id"]]
        if t["agent"] == GATE and st["status"] != "done" and _deps_done(t, state):
            if st["status"] != "awaiting_owner":
                st["status"] = "awaiting_owner"
                event(state, f"gate {t['id']} ready for the owner")
                notify(f"gate {t['id']} ready", f"{t['title']}\n{progress(state)}", level="all")
            gates.append(t)
    return gates


def dependents(doc: dict, task_ids: set[str]) -> list[str]:
    """Tasks that depend, directly or through other tasks, on one of ``task_ids``."""
    deps = {t["id"]: set(t.get("depends_on", [])) for t in doc["tasks"]}
    behind: set[str] = set()
    changed = True
    while changed:
        changed = False
        for tid, ds in deps.items():
            if tid not in behind and ds & (task_ids | behind):
                behind.add(tid)
                changed = True
    return [t["id"] for t in doc["tasks"] if t["id"] in behind]


def waiting_behind(doc: dict, state: dict, gate_ids: set[str]) -> list[str]:
    return [tid for tid in dependents(doc, gate_ids) if state["tasks"][tid]["status"] != "done"]


def external_reminder(doc: dict, state: dict) -> str:
    """Reminder lines for the external programs whose tasks are still to run, else ''."""
    lines = []
    for prog in C.EXTERNAL_PROGRAMS:
        ids = [t["id"] for t in doc["tasks"]
               if t["agent"] in prog.get("agents", []) and state["tasks"][t["id"]]["status"] != "done"]
        if ids:
            lines.append(f"REMINDER: {prog['name']} tasks still to run ({', '.join(ids)}). "
                         f"{prog.get('reminder', '')}".rstrip())
    return "\n".join(lines)


def open_provisional(state: dict) -> dict[str, dict]:
    return {q: p for q, p in state.get("provisional", {}).items() if p.get("status") == "open"}


def provisional_lines(state: dict) -> str:
    """Lines listing the provisional decisions awaiting the owner's ratification, else ''."""
    items = open_provisional(state)
    if not items:
        return ""
    listed = "; ".join(f"{q} (task {p['task']}, {p['file']})" for q, p in sorted(items.items()))
    return (f"PROVISIONAL: {len(items)} decision(s) taken by the resolver await ratification: "
            f"{listed}. Ratify or override each with harness/owner_tools.py.")


def write_status(doc: dict, state: dict) -> None:
    counts: dict[str, int] = {}
    rows, blocked, gates, reverify = [], [], [], []
    for t in doc["tasks"]:
        st = state["tasks"][t["id"]]
        counts[st["status"]] = counts.get(st["status"], 0) + 1
        shown = st["status"] + (" (reverify)" if st.get("reverify") else "")
        rows.append(f"| {t['id']} | {t['agent']} | {shown} | {st['attempts']} | {t['title']} |")
        if st.get("reverify"):
            reverify.append(f"- **{t['id']}**: owner reverify queued {st['reverify'].get('queued', '')}; "
                            "its committed work goes to the checks and the reviewer, without an agent")
        if st["status"] == "blocked":
            kind = st.get("block_type", "question")
            hint = f" {unblock_hint(t['id'])}" if kind in ("harness", "owner_action") else ""
            blocked.append(f"- **{t['id']}** ({kind}): {st.get('blocked_reason', '')}{hint}")
        if st["status"] == "awaiting_owner":
            gates.append(f"- **{t['id']}**: {t['title']}")
    prov = [f"- **{q}** (task {p['task']}): `{p['file']}`" for q, p in sorted(open_provisional(state).items())]
    questions = [f"- **{q}** (task {v['task']}, category {v.get('category') or '?'}): `{v['file']}` — "
                 f"`python harness/owner_tools.py decide qid={q} verdict=agree|amend|differ note=\"...\"`"
                 for q, v in sorted(open_questions_for_owner(state).items())]
    trust = [f"- {cat}: {e['status']} ({e['streak']} accepted in a row)"
             for cat, e in sorted(state.get("ledger", {}).items())] + [f"- {s}" for s in trust_suggestions(state)]
    header = f"Updated {now()}. " + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items()))
    if state.get("halted"):
        header += f"\n\n**HALTED:** {state['halted']}"
    lines = [
        f"# Status: {doc.get('milestone', '?')}", "", header, "",
        "| Task | Agent | Status | Attempts | Title |", "|---|---|---|---|---|", *rows, "",
        "## Gates awaiting the owner", *(gates or ["(none)"]), "",
        "## Provisional decisions awaiting ratification", *(prov or ["(none)"]), "",
        "## Questions waiting for the owner, with a recommendation", *(questions or ["(none)"]), "",
        "## Blocked", *(blocked or ["(none)"]), "",
        *(["## Owner reverify", *reverify, ""] if reverify else []),
        f"## Resolver trust (mode {C.RESOLVER_MODE})", *(trust or ["(no question yet)"]), "",
        "## Recent events", *[f"- {e}" for e in state["events"][-15:]], "",
    ]
    STATUS_FILE.write_text("\n".join(lines), encoding="utf-8")


def terminal(text: str, doc: dict, state: dict) -> str:
    """Add the reminder, provisional-decision and question lines to a terminal output, before
    NEXT ACTION."""
    extra = "\n".join(x for x in (external_reminder(doc, state), provisional_lines(state),
                                  question_lines(state)) if x)
    if not extra:
        return text
    head, sep, action = text.rpartition("\nNEXT ACTION:")
    return f"{head}\n{extra}{sep}{action}" if sep else f"{text}\n{extra}"


def halt(doc: dict, state: dict, reason: str) -> str:
    state["halted"] = reason
    state["loop_active"] = False
    event(state, f"HALT: {reason}")
    notify("loop halted", reason)
    save_state(doc, state)
    return terminal(f"HALTED: {reason}\nNEXT ACTION: report this to the owner in one or two lines "
                    "and end your turn.", doc, state)


GUARD_BLOCKS = HARNESS / "logs" / "guard_blocks.jsonl"


def guard_blocks_note(agent: str, since: float) -> str:
    """The calls of subagent ``agent`` the guard blocked since ``since`` (the guard records them
    in harness/logs/guard_blocks.jsonl), as a sentence for a step message; empty if none."""
    blocks = []
    try:
        for line in GUARD_BLOCKS.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            # role_source (0.5.3): a block of a main session started as this agent is not a call
            # of the subagent the loop spawned.
            if (entry.get("agent_type") == agent and entry.get("time", 0) >= since
                    and entry.get("role_source", "subagent") == "subagent"):
                blocks.append(entry)
    except OSError:
        return ""
    if not blocks:
        return ""
    last = blocks[-1]
    target = " ".join(str(last.get("target", "")).split())[:160]
    message = str(last.get("message", "")).split(" Do not work around this guard")[0]
    return (f" The guard blocked {len(blocks)} call(s) of the {agent} meanwhile; the last, "
            f"{last.get('tool', '?')} '{target}': {message} (harness/logs/guard_blocks.jsonl)")


def stop_requested_since(since: float) -> bool:
    """True when AGENT_STOP is present, or the guard refused a call because of it since ``since``
    (the owner may have deleted the file before the step script ran)."""
    if STOP_FILE.exists():
        return True
    try:
        lines = GUARD_BLOCKS.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("time", 0) >= since and str(entry.get("message", "")).startswith(STOP_DENIAL):
            return True
    return False


def stop_file() -> str:
    """The name of the stop file present at the repository root (the strongest), else ''."""
    for path in (EMERGENCY_STOP_FILE, STOP_FILE, SOFT_STOP_FILE):
        if path.exists():
            return path.name
    return ""


def spawn_or_stop(doc: dict, state: dict, head: str, then: str) -> str:
    """``head`` and the instruction to spawn the subagent the state expects, unless a stop file is
    present: then the loop stops before it. The pending spawn stays in the state and step_next
    gives it again when the loop restarts, so the task resumes where it stopped."""
    which = stop_file()
    if not which:
        return head + spawn_instruction(state, then)
    current = state["current"]
    agent = current["spawn"]["agent"]
    state["loop_active"] = False
    event(state, f"{which}: stopped before spawning the {agent} of {current['task_id']}")
    notify("loop stopped", f"{which}: {current['task_id']} stopped before its {agent}; it resumes "
           f"there.\n{progress(state)}")
    save_state(doc, state)
    return terminal(f"{head}STOPPED: {which} is present at the repository root; the '{agent}' "
                    f"subagent of {current['task_id']} was not spawned. Delete the file and start the "
                    f"loop again (/{C.LOOP_COMMAND}): it spawns that subagent first.\n"
                    "NEXT ACTION: tell the owner and end your turn.", doc, state)


def progress(state: dict) -> str:
    counts: dict[str, int] = {}
    for st in state["tasks"].values():
        counts[st["status"]] = counts.get(st["status"], 0) + 1
    others = ", ".join(f"{k} {v}" for k, v in sorted(counts.items()) if k != "done")
    return (f"Progress: {counts.get('done', 0)}/{len(state['tasks'])} done"
            f"{'; ' + others if others else ''}; dispatch {state.get('iterations', 0)}/{C.MAX_ITERATIONS}.")


def findings_path(task_id: str) -> Path:
    return FINDINGS_DIR / f"{task_id}.md"


#: Marker line opening each section of a findings file; kind is review, harness or done.
FINDINGS_MARK = re.compile(r"^<!-- laop-findings kind=(review|harness|done) attempt=(\d+) -->$", re.M)


def append_findings(task_id: str, kind: str, attempt: int, text: str) -> Path:
    """Append a section to the findings file of ``task_id`` (created with a title if missing)."""
    path = findings_path(task_id)
    FINDINGS_DIR.mkdir(parents=True, exist_ok=True)
    head = "" if path.exists() else f"# Findings for {task_id}\n"
    title = {"review": "reviewer verdict", "harness": "harness", "done": "done"}[kind]
    with path.open("a", encoding="utf-8") as f:
        f.write(f"{head}\n<!-- laop-findings kind={kind} attempt={attempt} -->\n"
                f"## Attempt {attempt}: {title} ({now()})\n\n{text.rstrip()}\n")
    return path


def open_findings(task_id: str) -> str:
    """The findings of ``task_id`` the next attempt must address: since the task was last done,
    the latest reviewer verdict and every harness finding after it (a later review supersedes the
    sections before it). Falls back to the legacy NEXT_FINDINGS.md when the task has no file."""
    path = findings_path(task_id)
    if not path.exists():
        if LEGACY_FINDINGS_FILE.exists():
            legacy = LEGACY_FINDINGS_FILE.read_text(encoding="utf-8")
            if legacy.startswith(f"# Findings for {task_id}"):
                return legacy.strip()
        return ""
    text = path.read_text(encoding="utf-8")
    marks = list(FINDINGS_MARK.finditer(text))
    sections = [(m.group(1), text[m.start():marks[i + 1].start() if i + 1 < len(marks) else len(text)])
                for i, m in enumerate(marks)]
    done = [i for i, (kind, _) in enumerate(sections) if kind == "done"]
    sections = sections[done[-1] + 1:] if done else sections
    reviews = [i for i, (kind, _) in enumerate(sections) if kind == "review"]
    sections = sections[reviews[-1]:] if reviews else sections
    return "\n".join(FINDINGS_MARK.sub("", body).strip() + "\n" for _, body in sections).strip()


def fail_attempt(doc: dict, state: dict, task: dict, findings: str, kind: str = "harness") -> None:
    """Count a rejected attempt and append its findings to harness/findings/<task>.md; ``kind`` is
    "review" for a reviewer verdict, "harness" for what the harness found."""
    st = state["tasks"][task["id"]]
    st["attempts"] += 1
    path = append_findings(task["id"], kind, st["attempts"], findings).relative_to(REPO).as_posix()
    if st["attempts"] >= C.MAX_ATTEMPTS_PER_TASK:
        st["status"] = "blocked"
        st["block_type"] = "harness"
        st["blocked_reason"] = f"rejected {st['attempts']} times; see {path}"
        event(state, f"{task['id']} blocked after {st['attempts']} rejected attempts")
        notify(f"{task['id']} blocked", f"{task['title']}\n{progress(state)}\n{unblock_hint(task['id'])}"
               f"\n\n{tail(findings, 30)}")
    else:
        st["status"] = "todo"
        event(state, f"{task['id']} attempt {st['attempts']} rejected")
        notify(f"{task['id']} attempt {st['attempts']} rejected",
               f"{task['title']}\nIt will be retried.\n{progress(state)}\n\n{tail(findings, 10)}", level="all")


def block(state: dict, task: dict, reason: str, block_type: str) -> None:
    st = state["tasks"][task["id"]]
    st.update(status="blocked", blocked_reason=reason, block_type=block_type)
    event(state, f"{task['id']} blocked ({block_type}): {reason[:200]}")
    hint = f"\n{unblock_hint(task['id'])}" if block_type in ("harness", "owner_action") else ""
    notify(f"{task['id']} needs you", reason + hint)


def mark_done(state: dict, task: dict, how: str, notes: str = "") -> None:
    st = state["tasks"][task["id"]]
    st["status"] = "done"
    if task["agent"] != GATE:
        st["done_sha"] = head_sha()
    if notes:
        st["reviewer_notes"] = tail(notes, 40)
    if findings_path(task["id"]).exists():  # closes the open findings; the history stays
        append_findings(task["id"], "done", st.get("attempts", 0), how)
    if LEGACY_FINDINGS_FILE.exists() and LEGACY_FINDINGS_FILE.read_text(encoding="utf-8").startswith(
            f"# Findings for {task['id']}"):
        LEGACY_FINDINGS_FILE.unlink()
    event(state, f"{task['id']} done ({how})")
    notify(f"{task['id']} done", f"{task['title']} ({how})\n{progress(state)}", level="all")


def fingerprint(state: dict) -> str:
    core = {
        "tasks": {k: [v["status"], v["attempts"]] for k, v in sorted(state["tasks"].items())},
        "current": state.get("current"),
        "iterations": state.get("iterations"),
        "halted": state.get("halted"),
    }
    return hashlib.sha256(json.dumps(core, sort_keys=True).encode()).hexdigest()


# --------------------------------------------------------------------------- guard checks


def guard_problem(script: str) -> str:
    """'' if the guard demonstrably intercepted the shell call that started ``script``, else the
    problem (F7: the heartbeat records the command)."""
    beat = load_json(HEARTBEAT, None) if HEARTBEAT.exists() else None
    if not beat:
        return "the guard hook has never run in this repository"
    age = time.time() - float(beat.get("time", 0))
    if beat.get("tool") not in ("Bash", "PowerShell") or age > HEARTBEAT_MAX_AGE_S:
        return (f"the guard hook did not run for this shell call (last run {age:.0f} s ago, "
                f"tool {beat.get('tool')!r})")
    if not re.search(rf"harness/{re.escape(script)}\.py\s*$", beat.get("command", "")):
        return f"the last shell call the guard saw was not this script ({beat.get('command', '')[:80]!r})"
    return ""


def refuse_without_guard(doc: dict, state: dict, script: str) -> str:
    problem = guard_problem(script)
    if not problem:
        return ""
    state["loop_active"] = False
    event(state, f"refused: {problem}")
    notify("guard not running", problem)
    save_state(doc, state)
    return (f"REFUSED: {problem}. The loop does not run without its guard. If you started this "
            "script by hand, that is expected: only the orchestrator runs it.\n"
            "NEXT ACTION: tell the owner and end your turn.")


# --------------------------------------------------------------------------- integrity (F4)


CACHE_DIRS = {"__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".hypothesis"}


def _tree_digest(path: Path) -> str:
    """Digest of what a task must not change in a git-ignored file or directory.

    - a file: its bytes;
    - a package directory (``site-packages``, recognised by its ``*.dist-info`` entries): the
      installed distributions (dist-info names) and the ``.pth`` files, so that installs, upgrades
      and removals are caught while the byte-code caches Python writes on import are not (0.2.2:
      the first import of a package by a task halted the loop);
    - any other directory: names, sizes and mtimes of its files, caches excluded.
    """
    if not path.exists():
        return "missing"
    if path.is_file():
        return file_hash(path)
    h = hashlib.sha256()
    try:
        entries = sorted(p.name for p in path.iterdir())
    except OSError:
        return "unreadable"
    if any(e.endswith(".dist-info") for e in entries):
        for name in entries:
            if name.endswith((".dist-info", ".egg-info")):
                h.update(f"{name}\n".encode())
            elif name.endswith(".pth"):
                h.update(f"{name}|{file_hash(path / name)}\n".encode())
        return h.hexdigest()
    for root, dirs, files in os.walk(path):
        dirs[:] = sorted(d for d in dirs if d not in CACHE_DIRS)
        for name in sorted(files):
            if name.endswith((".pyc", ".pyo")):
                continue
            p = Path(root) / name
            try:
                st = p.stat()
            except OSError:
                continue
            h.update(f"{p.relative_to(path).as_posix()}|{st.st_size}|{st.st_mtime_ns}\n".encode())
    return h.hexdigest()


def integrity_snapshot(state: dict) -> dict[str, str]:
    """Digests of what a dispatched task must not change but git does not see: the loop state,
    the review and resolution files, and the integrity paths of the config."""
    snap = {"harness/state.json": state_digest(state)}
    for rel in ("harness/verdicts", "harness/resolutions", "harness/findings", *C.INTEGRITY_PATHS):
        snap[rel] = _tree_digest(REPO / rel)
    return snap


def integrity_changes(before: dict[str, str], state_digest_now: str) -> list[str]:
    now_snap = {"harness/state.json": state_digest_now}
    for rel in before:
        if rel != "harness/state.json":
            now_snap[rel] = _tree_digest(REPO / rel)
    return [rel for rel, digest in before.items() if now_snap.get(rel) != digest]


# --------------------------------------------------------------------------- owner edits


OWNER_WRITES_FILE = HARNESS / "owner_writes.json"


def owner_paths() -> set[str]:
    """Files the owner edited in this working tree while a task was in flight, and that are still
    uncommitted (recorded by the guard). The task's checks ignore them: they are not its work."""
    try:
        return set(statefile.read(OWNER_WRITES_FILE).get("paths", []))
    except (OSError, ValueError):
        return set()


def claim_owner_commits(state: dict, task_id: str, recorded: set[str]) -> list[str]:
    """Commits of the task's current dispatch that touch only files the owner edited: the owner
    committed them from another session while the task ran. They become owner commits."""
    own = state["tasks"][task_id].get("own_commits", [])
    moved = []
    for sha in list(own):
        files = set(git("diff-tree", "--no-commit-id", "--name-only", "-r", "--root", sha).splitlines())
        if files and files <= recorded:
            own.remove(sha)
            state.setdefault("owner_commits", []).append(sha)
            moved.append(sha)
    return moved


def prune_owner_paths() -> set[str]:
    """Forget the owner's files that are no longer modified (the owner committed or reverted
    them); return the ones still pending."""
    pending = owner_paths()
    if not pending:
        return pending
    dirty = set(dirty_files())
    keep = pending & dirty
    if keep != pending:
        statefile.update(OWNER_WRITES_FILE, lambda d: d.update(paths=sorted(keep)))
    return keep


# --------------------------------------------------------------------------- prompts


def spawn(state: dict, agent: str, prompt_file: Path, kind: str, task_id: str) -> str:
    """Record the subagent the guard must accept next; return the token line of its prompt."""
    token_line = f"{C.PREFIX}-{kind} {task_id} {new_token()}"
    state["current"]["spawn"] = {"agent": agent, "token_line": token_line,
                                 "prompt_file": prompt_file.relative_to(REPO).as_posix()}
    return token_line


def spawn_instruction(state: dict, then: str) -> str:
    s = state["current"]["spawn"]
    return (f"NEXT ACTION: read {s['prompt_file']} and spawn the '{s['agent']}' subagent "
            f"(subagent_type='{s['agent']}', in the foreground: run_in_background=false if the Agent "
            f"tool has that parameter) with its full content as prompt, verbatim. When it returns, "
            f"run python harness/{then}.py")


def consume_steer(task_id: str = "") -> str:
    """Owner steer for the dispatch of ``task_id``: harness/STEER_<task_id>.md, then
    harness/STEER.md. Each file is injected once, then archived in harness/logs."""
    files = [HARNESS / f"STEER_{task_id}.md"] if task_id else []
    files.append(STEER_FILE)
    texts = []
    for path in files:
        if not path.exists():
            continue
        texts.append(path.read_text(encoding="utf-8").strip())
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        path.rename(LOG_DIR / f"{path.stem.lower()}_{dt.datetime.now():%Y%m%d_%H%M%S}.md")
    return "\n\n".join(t for t in texts if t)


def shown_check(check: dict) -> dict:
    """A check as the subagent must run it: placeholders of a cmd check (``python``, ``node``,
    any key of ``interpreters``) become the configured interpreters."""
    if "cmd" not in check:
        return check
    return {**check, "cmd": [C.INTERPRETERS.get(a, a) for a in check["cmd"]]}


def build_prompt(doc: dict, task: dict, token_line: str, steer: str, resolution: str = "",
                 resolution_by: str = "provisional", interrupted: str = "",
                 handoff: dict | None = None) -> str:
    findings = ""
    content = open_findings(task["id"])
    if content:
        findings = ("\nA previous attempt at this task was rejected. Address every finding below "
                    "before claiming review again (the latest reviewer verdict, then what the harness "
                    "found after it; the full history is in "
                    f"{findings_path(task['id']).relative_to(REPO).as_posix()}):\n\n{content}\n")
    if interrupted:
        findings += (f"\nThe previous session on this task was interrupted by an API error ({interrupted}) "
                     "before it wrote a claim; it did not count as an attempt. Its commits, if any, are "
                     "on the branch: check them and finish the task.\n")
    if handoff:
        findings += handoff_block(handoff)
    steer_block = f"\nOwner steering note (takes precedence over the task text):\n{steer}\n" if steer else ""
    resolution_block = ""
    if resolution and resolution_by == "owner":
        resolution_block = (
            "\nThe owner decided your question by accepting the recommendation below (the "
            "resolver's answer and the challenger's section; amendments, if any, are part of it), "
            "plus any owner note in the steering note below. Apply it. Record it in "
            f"{C.OPEN_QUESTIONS_REL} under the question as 'Status: decided (owner, "
            f"{dt.date.today()})' with the decision text, in your commit:\n\n{resolution}\n")
    elif resolution and resolution_by == "ratified":
        resolution_block = (
            "\nYour question was answered by the resolver and confirmed by the challenger, and the "
            "owner has already ratified the decision. Apply it and every amendment of the "
            f"challenger (they are part of the decision). Record it in {C.OPEN_QUESTIONS_REL}, in "
            "the entry of the question, with the decision text and the line 'Status: decided "
            f"(ratified by the owner, {dt.date.today()})', in your commit:\n\n{resolution}\n")
    elif resolution:
        resolution_block = (
            "\nYour question was answered by the resolver and confirmed by the challenger, on the "
            "owner's behalf and provisionally (the owner ratifies it later). Apply the decision and "
            "every amendment of the challenger (they are part of the decision). Record it in "
            f"{C.OPEN_QUESTIONS_REL}, in the entry of the question (its '## <question id>:' "
            "section), with the decision text and these two lines exactly as written, each on a "
            "line of its own, unwrapped (the owner's ratification rewrites them; write no other "
            f"status line):\n\n{PROVISIONAL_STATUS.format(date=dt.date.today())}\n"
            f"{PENDING_DECISION}\n\nCommit it with your work. The decision:\n\n{resolution}\n")
    checks = json.dumps([shown_check(c) for c in C.GLOBAL_CHECKS + task["acceptance"]], indent=2)
    refs = "\n".join(f"- {r}" for r in task.get("spec_refs", [])) or "- (none)"
    return (
        f"{token_line}\n\n"
        f"Task {task['id']}: {task['title']}\n"
        f"Milestone: {doc.get('milestone', '?')}. Your role: {task['agent']}. "
        f"Branch: {current_branch()}.\n\n"
        f"Task description:\n{task.get('description', '')}\n\n"
        f"Spec references:\n{refs}\n\n"
        "After you claim 'review', the harness independently executes these checks "
        "(default FAIL: pytest checks must collect and pass the stated minimum number of "
        f"tests, skipped tests do not count):\n{checks}\n"
        f"{findings}{resolution_block}{steer_block}\n"
        "Follow CLAUDE.md and your agent instructions. Work on this task only. Finish by "
        "committing, then writing harness/claim.json. Your final reply is at most 5 lines."
    )


def handoff_block(handoff: dict) -> str:
    """The prompt section of a task whose previous session the owner stopped (AGENT_STOP)."""
    if handoff.get("done") or handoff.get("todo"):
        note = (f"What it did: {handoff.get('done') or '(not stated)'}\n"
                f"What is left: {handoff.get('todo') or '(not stated)'}\n")
    else:
        note = "It wrote no handoff note: work out from its commits what is left.\n"
    if handoff.get("uncommitted"):
        note += f"It left these files uncommitted: {', '.join(handoff['uncommitted'])}\n"
    return ("\nThe previous session on this task was stopped by the owner before it finished; it "
            "did not count as an attempt. Continue from where it left off: do not redo committed "
            f"work, build on its WIP commits.\n{note}"
            f"The task's commits so far (git log --oneline):\n{handoff.get('log') or '(none)'}\n")


def build_review_prompt(task: dict, base: str, verdict_rel: str, token_line: str,
                        excluded: list[str] | tuple[str, ...] = (),
                        owner_files: list[str] | tuple[str, ...] = ()) -> str:
    note = ""
    if excluded:
        note = ("The range contains commits that are not part of this task (owner commits or "
                "commits of other tasks): " + ", ".join(excluded)
                + ". Ignore their changes (see `git show --stat <sha>`); review every other commit.\n\n")
    if owner_files:
        note += ("The owner edited these files in the same working tree while the task ran; they are "
                 "not the task's work, ignore them (but flag it if the task's commits include them): "
                 + ", ".join(sorted(owner_files)) + ".\n\n")
    return (
        f"{token_line}\n\n"
        f"Review task {task['id']} ({task['agent']} task; you are the "
        f"{task.get('reviewer', 'evaluator')}): {task['title']}.\n\n"
        f"Task description:\n{task.get('description', '')}\n\n"
        f"Spec references: {', '.join(task.get('spec_refs', [])) or '(none)'}\n\n"
        f"The full change set is `git diff {base}..HEAD`. The harness has run the acceptance and "
        "global checks; they passed. That says nothing about whether the tests can fail or test the "
        "right thing: review according to your instructions.\n\n"
        f"{note}"
        f"Write your complete review to {verdict_rel}. Its first line must be exactly "
        "'VERDICT: PASS' or 'VERDICT: NEEDS_WORK'. Your final reply is only that first line."
    )


def build_resolve_prompt(task: dict, claim: dict, token_line: str, out_rel: str) -> str:
    return (
        f"{token_line}\n\n"
        f"The '{task['agent']}' working on task {task['id']} ({task['title']}) stopped on a question "
        f"({claim.get('question_id') or 'no id'}).\n\n"
        f"Question:\n{claim.get('question', '')}\n\n"
        f"Agent's summary and notes:\n{claim.get('summary', '')}\n{claim.get('notes_for_owner', '')}\n\n"
        f"Task description:\n{task.get('description', '')}\n\n"
        f"The full entry is in {C.OPEN_QUESTIONS_REL}. The owner's principles, reserved and delegated "
        f"categories and escalation signals are in {C.PRINCIPLES_REL}: read them first.\n\n"
        f"Write your resolution to {out_rel}, in the format of your instructions: first line exactly "
        "'RESOLUTION: ANSWER' or 'RESOLUTION: ESCALATE', then a 'Category:' line with the id of the "
        f"reserved (R) or delegated (D) category of {C.PRINCIPLES_REL} the question belongs to, and "
        "your recommended decision in both cases. Your final reply is only the first line."
    )


def build_challenge_prompt(task: dict, claim: dict, token_line: str, out_rel: str,
                           escalated: bool = False) -> str:
    what = ("judged that the question belongs to the owner and wrote a recommendation for the owner"
            if escalated else "answered, on the owner's behalf,")
    focus = ("The question goes to the owner in any case: challenge the recommendation the owner will "
             "read (AGREE, AMEND or COUNTER).\n\n" if escalated else "")
    return (
        f"{token_line}\n\n"
        f"The resolver {what} the question raised by the '{task['agent']}' "
        f"on task {task['id']} ({task['title']}). The question and the resolution are in {out_rel}; "
        f"the full question entry in {C.OPEN_QUESTIONS_REL}; the owner's principles in "
        f"{C.PRINCIPLES_REL}.\n\n{focus}"
        f"Challenge it according to your instructions and append your challenge at the end of "
        f"{out_rel} (do not change the resolver's text). Your section starts with a line exactly "
        "'CHALLENGE: AGREE', 'CHALLENGE: AMEND', 'CHALLENGE: COUNTER' or 'CHALLENGE: ESCALATE'. Your "
        "final reply is only that line."
    )


# --------------------------------------------------------------------------- checks (F3)


def junit_counts(path: Path) -> tuple[int, int, int, int]:
    """Return (tests, failures, errors, skipped) from a pytest junit XML file."""
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    totals = [0, 0, 0, 0]
    for s in suites:
        for i, key in enumerate(("tests", "failures", "errors", "skipped")):
            totals[i] += int(s.get(key, 0))
    return tuple(totals)  # type: ignore[return-value]


def kill_tree(pid: int) -> None:
    """Kill a process and every process it started (pytest-xdist workers, shells...)."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, timeout=60)
        else:
            import signal
            os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass


def run_command(cmd: list[str], timeout: float, out_file: Path) -> tuple[int | None, str]:
    """Run ``cmd`` in the repository with its output in ``out_file``; return (exit code, output).
    On timeout the whole process tree is killed and the exit code is None."""
    group = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
             else {"start_new_session": True})
    with open(out_file, "wb") as fh:
        proc = subprocess.Popen(cmd, cwd=REPO, stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
                                **group)
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_tree(proc.pid)
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
            code = None
    return code, out_file.read_bytes().decode("utf-8", "replace")


def run_checks(task: dict, attempt: int, started: float | None = None,
               on_report=None) -> tuple[bool, str]:
    """Run the global and acceptance checks within the time budget (CHECK_BUDGET_MIN, counted in
    wall-clock time from ``started``): a check that cannot start or finish within the remaining
    budget fails the attempt. ``on_report(i, ok, report)`` is called after each check."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    reports, all_ok = [], True
    deadline = (started or time.time()) + C.CHECK_BUDGET_MIN * 60
    for i, check in enumerate(C.GLOBAL_CHECKS + task["acceptance"]):
        remaining = deadline - time.time()
        timeout = min(C.CHECK_TIMEOUT_MIN * 60, remaining)
        out = ""
        if timeout <= 5:
            ok, detail = False, (f"not run: the time budget of {C.CHECK_BUDGET_MIN} min for all the "
                                 "checks of a task is exhausted (harness/project.json, check_budget_min)")
        else:
            out_file = LOG_DIR / f"check_{task['id']}_{attempt}_{i}.out"
            try:
                if "pytest" in check:
                    junit = LOG_DIR / f"junit_{task['id']}_{attempt}_{i}.xml"
                    junit.unlink(missing_ok=True)
                    python = C.INTERPRETERS.get(check.get("interpreter", "python"), C.PYTHON)
                    cmd = [python, "-m", "pytest", *check["pytest"], "-q", f"--junitxml={junit}"]
                    code, out = run_command(cmd, timeout, out_file)
                    if code is None:
                        ok, detail = False, f"timeout after {timeout / 60:.1f} min"
                    elif code == 5 and check.get("allow_empty"):
                        ok, detail = True, "no tests collected (allowed)"
                    elif not junit.exists():
                        ok, detail = False, f"pytest exit code {code}, no junit report"
                    else:
                        tests, failures, errors, skipped = junit_counts(junit)
                        passed = tests - failures - errors - skipped
                        need = check.get("min_passed", 1)
                        ok = code == 0 and passed >= need
                        detail = (f"passed={passed} (min {need}), failures={failures}, errors={errors}, "
                                  f"skipped={skipped}, exit={code}")
                elif "cmd" in check:
                    code, out = run_command(shown_check(check)["cmd"], timeout, out_file)
                    ok = code == 0
                    detail = f"timeout after {timeout / 60:.1f} min" if code is None else f"exit={code}"
                else:
                    ok, detail = False, "unknown check type (use 'pytest' or 'cmd')"
            except (FileNotFoundError, PermissionError) as exc:
                ok, detail = False, f"cannot run check: {exc}"
        all_ok &= ok
        body = "" if ok or not out else f"```\n{tail(out)}\n```\n"
        report = f"### [{'OK' if ok else 'FAIL'}] {json.dumps(check)}\n{detail}\n{body}"
        reports.append(report)
        if on_report:
            on_report(i, ok, report)
    return all_ok, "\n".join(reports)


# --------------------------------------------------------------------------- resolver


PLACEHOLDER = re.compile(r"\(to be written by the owner\)|\{\{[A-Z_]+\}\}")
CATEGORY_ROW = re.compile(r"^\|\s*([RD]\d+)\s*\|", re.MULTILINE)
LEDGER_HISTORY = 40


def principle_categories() -> set[str]:
    """Ids of the reserved (R) and delegated (D) categories of the principles file."""
    text = PRINCIPLES.read_text(encoding="utf-8") if PRINCIPLES.exists() else ""
    return set(CATEGORY_ROW.findall(text))


def principles_problem() -> str:
    """'' if the principles file can guide the resolver, otherwise why it cannot."""
    if not PRINCIPLES.exists():
        return f"{C.PRINCIPLES_REL} does not exist"
    if PLACEHOLDER.search(PRINCIPLES.read_text(encoding="utf-8")):
        return f"{C.PRINCIPLES_REL} still has placeholders to fill"
    if not principle_categories():
        return f"{C.PRINCIPLES_REL} defines no reserved (R) or delegated (D) categories"
    return ""


def may_resolve(state: dict, task_id: str) -> bool:
    """The resolver phases run for this question (mode advise or auto, a usable principles file,
    caps not reached)."""
    if not C.RESOLVER_ENABLED or principles_problem():
        return False
    st = state["tasks"][task_id]
    return (state.get("resolutions_this_run", 0) < C.MAX_RESOLUTIONS_PER_RUN
            and len(st.get("resolutions", [])) < C.MAX_RESOLUTIONS_PER_TASK)


def resolution_text(path: Path) -> str:
    """The resolution file (the resolver's answer and the challenger's section)."""
    return path.read_text(encoding="utf-8").strip() if path.exists() else ""


def resolution_field(text: str, name: str) -> str:
    """The value of the first line ``<name>: value`` of a resolution, or ''."""
    m = re.search(rf"(?mi)^\s*{re.escape(name)}\s*:\s*(.+?)\s*$", text)
    return m.group(1) if m else ""


def question_category(text: str) -> str:
    """The category the resolver assigned (a known R or D id), or '' when missing or unknown:
    an unclassified question is the owner's."""
    m = re.match(r"([RD]\d+)\b", resolution_field(text, "Category").upper())
    return m.group(1) if m and m.group(1) in principle_categories() else ""


# Trust ledger (state["ledger"], carried over by owner_tools archive). One entry per category:
# status "owner" (every question of the category is decided by the owner, with the recommendation),
# "warmup" (a delegated category whose answers still go to the owner until `warmup` of them in a
# row were accepted), "auto" (decided provisionally when resolver and challenger agree). Reserved
# categories start as "owner" and are never promoted automatically; an override of a provisional
# decision sends its category back to "warmup".


def ledger_entry(state: dict, category: str) -> dict:
    led = state.setdefault("ledger", {})
    if category not in led:
        led[category] = {"status": "warmup" if category.startswith("D") else "owner", "streak": 0,
                         "obvious": 0, "history": []}
    return led[category]


def category_status(state: dict, category: str) -> str:
    return ledger_entry(state, category)["status"] if category else "owner"


def ledger_record(state: dict, category: str, qid: str, task_id: str, kind: str) -> None:
    """Record an outcome and apply the trust rules. ``kind``: agree, amend, differ (the owner's
    decision against the recommendation), obvious (an escalated question the owner says the
    resolver could have decided), ratified, overridden (of a provisional decision)."""
    if not category:
        return
    e = ledger_entry(state, category)
    e["history"] = (e["history"] + [{"q": qid, "task": task_id, "kind": kind, "time": now()}])[-LEDGER_HISTORY:]
    if kind in ("agree", "amend", "ratified"):
        e["streak"] += 1
    elif kind in ("differ", "overridden"):
        e["streak"] = 0
    elif kind == "obvious":
        e["obvious"] += 1
    if kind == "overridden" and e["status"] == "auto":
        e["status"] = "warmup"
        event(state, f"category {category} back to warm-up: the owner overrode {qid}")
        notify(f"category {category} back to warm-up", f"The owner overrode {qid}.")
    elif e["status"] == "warmup" and e["streak"] >= C.RESOLVER_WARMUP:
        e["status"] = "auto"
        event(state, f"category {category} autonomous after {e['streak']} accepted answers")
        notify(f"category {category} autonomous", f"After {e['streak']} accepted answers the resolver "
               f"decides {category} questions provisionally. Undo: owner_tools.py trust category={category} "
               "level=warmup", level="all")


def trust_suggestions(state: dict) -> list[str]:
    """Reserved categories whose escalations the owner repeatedly found obvious."""
    return [f"{cat}: {e['obvious']} escalated questions the owner found obvious; consider delegating it "
            f"in {C.PRINCIPLES_REL}" for cat, e in sorted(state.get("ledger", {}).items())
            if e["status"] == "owner" and e.get("obvious", 0) >= 3]


def open_questions_for_owner(state: dict) -> dict[str, dict]:
    return {q: v for q, v in state.get("questions", {}).items() if v.get("status") == "open"}


def question_lines(state: dict) -> str:
    """Lines listing the questions waiting for the owner with a prepared recommendation, else ''."""
    items = open_questions_for_owner(state)
    if not items:
        return ""
    listed = "; ".join(f"{q} (task {v['task']}, {v['file']})" for q, v in sorted(items.items()))
    return (f"QUESTIONS: {len(items)} question(s) wait for the owner with a recommendation: {listed}. "
            "Decide each with harness/owner_tools.py decide.")


# --------------------------------------------------------------------------- open-questions entries


# The fixed lines the builder writes for a provisional decision (build_prompt) and that ratify
# rewrites. A status line may wrap onto continuation lines; optional bold markers are kept.
PROVISIONAL_STATUS = "Status: provisional (resolver, {date})"
PENDING_DECISION = "**Decision (owner):** pending ratification."
_STATUS_RE = re.compile(r"(\*\*)?Status:(\*\*)?\s+provisional\s+\(resolver,\s+(\d{4}-\d{2}-\d{2})\)")
_PENDING_RE = re.compile(r"\*\*Decision\s+\(owner\):\*\*\s+pending\s+ratification\.")
_STALE_RE = re.compile(r"Status:(?:\*\*)?\s+provisional|pending\s+ratification", re.I)


def question_key(key: str) -> str:
    """The question id of a provisional key (``Q3`` or ``Q3@M1-002#2`` for a repeated id)."""
    return key.split("@", 1)[0]


def entry_span(text: str, qid: str) -> tuple[int, int] | None:
    """Start and end offsets of the entry of ``qid`` in the open-questions file: from its
    ``## <qid>:`` heading to the next ``## `` heading. None when there is not exactly one."""
    heads = [m.start() for m in re.finditer(rf"(?m)^##[ \t]+{re.escape(qid)}:", text)]
    if len(heads) != 1:
        return None
    nxt = re.compile(r"(?m)^##[ \t]").search(text, heads[0] + 2)
    return heads[0], nxt.start() if nxt else len(text)


def stale_entries(state: dict) -> list[str]:
    """The questions that state.json records as ratified or overridden whose entry in the
    open-questions file still says provisional or pending ratification."""
    closed = sorted({question_key(k) for k, p in state.get("provisional", {}).items()
                     if p.get("status") in ("ratified", "overridden")})
    if not closed or not OPEN_QUESTIONS.exists():
        return []
    text = OPEN_QUESTIONS.read_text(encoding="utf-8", errors="replace")
    stale = []
    for qid in closed:
        span = entry_span(text, qid)
        if span and _STALE_RE.search(text, *span):
            stale.append(qid)
    return stale


def ratify_entry(key: str) -> list[str]:
    """Record the owner's ratification of ``key`` in the open-questions file (tracked, the durable
    record): the provisional status line(s) and the pending decision line of its entry are
    rewritten in place, line endings kept. Nothing is guessed: when the entry or the lines are not
    there, nothing is edited and the result is a REMINDER. Returns the lines to report; the first
    starts with "updated " when the file was edited. The owner commits it, never the tool."""
    qid, rel = question_key(key), C.OPEN_QUESTIONS_REL
    today = dt.date.today().isoformat()
    remind = (f"REMINDER: edit the entry of {qid} in {rel} by hand: set its status to 'Status: decided "
              f"(ratified by the owner, {today})' and its owner decision to ratified")
    try:
        with open(OPEN_QUESTIONS, encoding="utf-8", newline="") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError):
        return [f"{remind} ({rel} could not be read)."]
    span = entry_span(text, qid)
    if not span:
        return [f"{remind} (no single '## {qid}:' heading found)."]
    start, end = span
    body = text[start:end]
    body, n_status = _STATUS_RE.subn(
        lambda m: (f"{m[1] or ''}Status:{m[2] or ''} decided (ratified by the owner, {today}; "
                   f"provisional decision of the resolver, {m[3]})"), body)
    if not n_status:
        return [f"{remind} (no 'Status: provisional (resolver, <date>)' line in the entry)."]
    body, n_pending = _PENDING_RE.subn(
        f"**Decision (owner):** ratified, {today}: the resolver's decision below, with the "
        "challenger's amendments (if any).", body)
    with open(OPEN_QUESTIONS, "w", encoding="utf-8", newline="") as f:
        f.write(text[:start] + body + text[end:])
    out = [f"updated {rel}, entry {qid}: {n_status} status line(s)"
           + (f" and {n_pending} owner decision line(s)" if n_pending else "")
           + " set to ratified. Commit it (the tool does not commit)."]
    if _STALE_RE.search(body):
        out.append(f"REMINDER: the entry of {qid} in {rel} still says provisional or pending "
                   "ratification elsewhere: edit it by hand.")
    return out


def record_owner_write(state: dict, rel: str) -> bool:
    """Record a file an owner action wrote in the working tree (as the guard does for the owner's
    own edits), so that the task in flight is not charged with it. False: the loop is not
    active, nothing to record."""
    if not (state.get("current") or state.get("loop_active")):
        return False
    statefile.update(OWNER_WRITES_FILE, lambda d: d.update(
        paths=sorted(set(d.get("paths", [])) | {rel})))
    return True


# --------------------------------------------------------------------------- owner actions


OWNER_ACTIONS = ("pass_gate", "unblock", "resume", "reverify", "owner_commit", "foreign_commit",
                 "allow_path", "clear_halt", "release", "ratify", "override", "decide", "trust")
DECIDE_VERDICTS = ("agree", "amend", "differ")
TRUST_LEVELS = ("owner", "warmup", "auto")


def enqueue_owner_action(entry: dict) -> None:
    def change(queue: dict) -> None:
        queue.setdefault("actions", []).append({**entry, "queued": now()})
    statefile.update(QUEUE_FILE, change)


def drain_owner_queue(doc: dict, state: dict) -> list[str]:
    """Apply the queued owner actions to ``state`` (in order) and empty the queue."""
    with statefile.locked(QUEUE_FILE):
        queue = statefile.read(QUEUE_FILE)
        actions = queue.get("actions", [])
        if actions:
            statefile.write(QUEUE_FILE, {"actions": []})
    results = []
    for a in actions:
        result = apply_owner_action(doc, state, a)
        if not result.endswith(" done."):
            event(state, f"owner action not applied: {result}")
        results.append(result)
    return results


TASK_ACTIONS = ("pass_gate", "unblock", "resume", "reverify", "foreign_commit", "allow_path")


def owner_action_problem(doc: dict, state: dict, a: dict) -> str:
    """'' when the owner action ``a`` can be applied to ``state``, else why not (one line).

    owner_tools checks it before queueing or applying, so that a malformed action is refused at
    once instead of being reported as applied and dropped later by the step script."""
    action, task_id = a.get("action"), a.get("task_id")
    if action not in OWNER_ACTIONS:
        return f"Unknown action {action!r}."
    if action in TASK_ACTIONS:
        if not task_id:
            return f"{action}: set task=<task id>."
        if task_id not in state["tasks"] or not any(t["id"] == task_id for t in doc["tasks"]):
            return f"{action}: unknown task id {task_id!r}."
    if action == "pass_gate":
        task = task_by_id(doc, task_id)
        if task["agent"] != GATE:
            return f"{task_id} is not a gate (agent {task['agent']!r})."
        if task.get("final") and open_provisional(state):
            return (f"{task_id} is the final gate: ratify or override the provisional decisions first "
                    f"({', '.join(sorted(open_provisional(state)))}).")
        stale = stale_entries(state) if task.get("final") else []
        if stale:
            return (f"{task_id} is the final gate: {C.OPEN_QUESTIONS_REL} still says provisional or "
                    f"pending ratification for {', '.join(stale)}, closed by the owner. Update those "
                    "entries ('Status: decided ...'), commit, then pass the gate.")
    elif action == "reverify":
        return reverify_problem(doc, state, task_id)
    elif action in ("owner_commit", "foreign_commit"):
        if not a.get("commit"):
            return f"{action}: set commit=<sha>."
        try:
            git("rev-parse", "--verify", f"{a['commit']}^{{commit}}")
        except LoopError:
            return f"{action}: {a['commit']!r} is not a commit of this repository."
    elif action == "allow_path":
        if not a.get("path"):
            return "allow_path: set path=<relative path>."
    elif action in ("ratify", "override"):
        qid = a.get("qid")
        if not qid:
            return f"{action}: set qid=<question key>."
        prov = state.get("provisional", {}).get(qid)
        if not prov or prov.get("status") != "open":
            return f"{action}: no open provisional decision {qid!r}."
    elif action == "decide":
        qid, verdict = a.get("qid"), a.get("verdict")
        if not qid:
            return "decide: set qid=<question key>."
        q = state.get("questions", {}).get(qid)
        if not q or q.get("status") != "open":
            return f"decide: no question {qid!r} waits for a decision (see 'status')."
        if verdict not in DECIDE_VERDICTS:
            return f"decide: verdict must be one of {'|'.join(DECIDE_VERDICTS)}."
        if verdict in ("amend", "differ") and not (a.get("note") or "").strip():
            return f"decide: verdict {verdict} needs note=\"the owner's decision\"."
    elif action == "trust":
        if not a.get("category") or a.get("level") not in TRUST_LEVELS:
            return f"trust: set category=<R or D id> and level={'|'.join(TRUST_LEVELS)}."
    return ""


def apply_owner_action(doc: dict, state: dict, a: dict) -> str:
    """Apply one owner action to the state; return a one-line result."""
    action, task_id = a.get("action"), a.get("task_id")
    problem = owner_action_problem(doc, state, a)
    if problem:
        return problem
    if action == "pass_gate":
        task = task_by_id(doc, task_id)
        open_deps = [d for d in task.get("depends_on", []) if state["tasks"][d]["status"] != "done"]
        mark_done(state, task, "gate passed by the owner"
                  + (f" before {', '.join(open_deps)} were done" if open_deps else ""))
    elif action == "unblock":
        st = state["tasks"][task_id]
        st.update(status="todo", attempts=0, blocked_reason="")
        st.pop("base_sha", None)  # a fresh base at the next dispatch
        event(state, f"owner unblocked {task_id}")
    elif action == "resume":
        state["tasks"][task_id].update(status="todo", blocked_reason="", resumed=True)
        event(state, f"owner resumed {task_id} (base kept)")
    elif action == "reverify":
        st = state["tasks"][task_id]
        st.update(status="todo", blocked_reason="", reverify={"claim": last_claim(state, task_id),
                                                              "queued": a.get("queued") or now()})
        st.pop("resumed", None)
        event(state, f"owner reverify {task_id}: its committed work goes to the checks and the "
                     "reviewer, without a new task agent")
    elif action in ("owner_commit", "foreign_commit"):
        sha = git("rev-parse", "--verify", f"{a['commit']}^{{commit}}")
        target = (state.setdefault("owner_commits", []) if action == "owner_commit"
                  else state["tasks"][task_id].setdefault("foreign_commits", []))
        if sha not in target:
            target.append(sha)
        event(state, f"{action} recorded: {sha}" + (f" for {task_id}" if task_id else ""))
    elif action == "allow_path":
        allowed = state["tasks"][task_id].setdefault("allowed_paths", [])
        if a["path"] not in allowed:
            allowed.append(a["path"])
        event(state, f"owner accepted {a['path']} for {task_id}")
    elif action == "clear_halt":
        state.update(halted=None, empty_runs=0)
        cur = state.get("current")
        if cur and cur.get("phase") == "awaiting_review":
            cur.update(review_tries=0, verify_time=time.time())
        event(state, "owner cleared the halt")
    elif action == "release":
        state["loop_active"] = False
        event(state, "owner released the orchestrator binding")
    elif action in ("ratify", "override"):
        qid = a.get("qid")
        prov = state.get("provisional", {}).get(qid)
        if not prov or prov.get("status") != "open":
            return f"{action}: no open provisional decision {qid!r}."
        if action == "ratify":
            prov.update(status="ratified", closed=now())
            ledger_record(state, prov.get("category", ""), qid, prov["task"], "ratified")
            event(state, f"owner ratified provisional decision {qid}")
            report = ratify_entry(qid)
            if report[0].startswith("updated "):
                record_owner_write(state, C.OPEN_QUESTIONS_REL)
            for line in report:
                event(state, line)
            if state.get("current") and any(x.startswith("REMINDER") for x in report):
                notify(f"{qid} ratified", "\n".join(report))  # applied from the queue: the owner is away
        else:
            ledger_record(state, prov.get("category", ""), qid, prov["task"], "overridden")
            # the task that resumed with the decision, and every task built on it since
            reopened = [prov["task"]] + [
                t for t in dependents(doc, {prov["task"]})
                if state["tasks"][t]["status"] in ("done", "in_progress") and task_by_id(doc, t)["agent"] != GATE]
            for tid in reopened:
                st = state["tasks"][tid]
                st.update(status="todo", attempts=0)
                st.pop("base_sha", None)
                note = (a.get("note") or "").strip()
                (HARNESS / f"STEER_{tid}.md").write_text(
                    f"The owner overrode the provisional decision {qid}. "
                    + (f"Owner's decision: {note}\n" if note else "See the owner's decision in "
                       f"{C.OPEN_QUESTIONS_REL}.\n")
                    + "Rework what this task built on the provisional decision accordingly, and update "
                      f"the entry of {question_key(qid)} in {C.OPEN_QUESTIONS_REL}: replace its "
                      f"'Status: provisional (resolver, <date>)' line with 'Status: decided (owner, "
                      f"{dt.date.today()})' and its line '{PENDING_DECISION}' with "
                      "'**Decision (owner):** ' and the owner's decision.\n", encoding="utf-8")
            prov.update(status="overridden", closed=now(), reopened=reopened)
            event(state, f"owner overrode {qid}; reopened {', '.join(reopened)}")
    elif action == "decide":
        return decide(state, a)
    elif action == "trust":
        cat, level = a["category"].upper(), a["level"]
        e = ledger_entry(state, cat)
        e.update(status=level, streak=0)
        event(state, f"owner set category {cat} to {level}")
    else:
        return f"Unknown action {action!r}."
    return f"{action} done."


def decide(state: dict, a: dict) -> str:
    """The owner's decision on a question that waits with a recommendation.

    ``verdict``: agree (apply the recommendation as written), amend (apply it with the owner's
    ``note``), differ (the owner decides otherwise, in ``note``). ``obvious=yes`` records that the
    resolver could have decided it. The task resumes with the decision in its next prompt.
    """
    qid, verdict, note = a.get("qid"), a.get("verdict"), (a.get("note") or "").strip()
    q = state.get("questions", {}).get(qid)
    if not q or q.get("status") != "open":
        return f"decide: no question {qid!r} waits for a decision (see 'status')."
    if verdict not in DECIDE_VERDICTS:
        return f"decide: verdict must be one of {'|'.join(DECIDE_VERDICTS)}."
    if verdict in ("amend", "differ") and not note:
        return f"decide: verdict {verdict} needs note=\"the owner's decision\"."
    st = state["tasks"][q["task"]]
    if verdict in ("agree", "amend"):
        st["pending_resolution"] = q["file"]
        st["pending_resolution_by"] = "owner"
    if note:
        label = "Owner's amendments to the recommendation" if verdict == "amend" else "Owner's decision"
        (HARNESS / f"STEER_{q['task']}.md").write_text(
            f"{label} on {qid}: {note}\nRecord it in {C.OPEN_QUESTIONS_REL} under {qid} as "
            f"'Status: decided (owner, {dt.date.today()})'.\n", encoding="utf-8")
    st.update(status="todo", blocked_reason="")
    q.update(status="decided", verdict=verdict, closed=now())
    ledger_record(state, q.get("category", ""), qid, q["task"], verdict)
    if str(a.get("obvious", "")).lower() in ("yes", "true", "1"):
        ledger_record(state, q.get("category", ""), qid, q["task"], "obvious")
    event(state, f"owner decided {qid} ({verdict}); {q['task']} resumes")
    return "decide done."


# --------------------------------------------------------------------------- reverify


#: Claim fields kept in the state (``last_claim``) for a later ``reverify``.
CLAIM_FIELDS = ("status", "summary", "notes_for_owner", "block_type")

UNBLOCK_HINT = ("After the fix: 'owner_tools.py reverify task={tid}' sends the committed work straight to "
                "the checks and the reviewer (fast path); 'resume task={tid}' dispatches the agent again "
                "when it must change its work.")


def unblock_hint(task_id: str) -> str:
    return UNBLOCK_HINT.format(tid=task_id)


def remember_claim(state: dict, task_id: str, claim: dict, attempt: int) -> None:
    """Keep the task's last valid claim in the state: claim.json is deleted at the next dispatch."""
    state["tasks"][task_id]["last_claim"] = {
        **{k: claim[k] for k in CLAIM_FIELDS if claim.get(k)}, "attempt": attempt, "time": now()}


def last_claim(state: dict, task_id: str) -> dict | None:
    """The task's last claim with a summary: kept in the state, else harness/claim.json when it is
    still the task's (a claim written before laop 0.4.3, which did not keep it)."""
    kept = state["tasks"].get(task_id, {}).get("last_claim")
    if isinstance(kept, dict) and str(kept.get("summary") or "").strip():
        return kept
    try:
        claim = load_json(CLAIM_FILE) if CLAIM_FILE.exists() else None
    except ValueError:
        claim = None
    if isinstance(claim, dict) and claim.get("task_id") == task_id and str(claim.get("summary") or "").strip():
        return {k: claim[k] for k in CLAIM_FIELDS if claim.get(k)}
    return None


def reverify_problem(doc: dict, state: dict, task_id: str) -> str:
    """'' when ``task_id`` may be reverified (its committed work checked and reviewed again without
    a new task agent), else why not."""
    task = task_by_id(doc, task_id)
    st = state["tasks"][task_id]
    if task["agent"] == GATE:
        return f"reverify: {task_id} is a gate: pass it with pass_gate."
    if (state.get("current") or {}).get("task_id") == task_id:
        return f"reverify: {task_id} is in flight; wait for its step to finish."
    if st["status"] == "blocked" and st.get("block_type", "question") == "question":
        return (f"reverify: {task_id} is blocked on a question; a question goes through the resolver "
                "or the owner's decision (decide), then the task resumes.")
    if not ((st["status"] == "blocked" and st.get("block_type") in ("harness", "owner_action"))
            or (st["status"] == "todo" and st.get("resumed"))):
        return (f"reverify: {task_id} is '{st['status']}'; only a task blocked with block_type harness "
                "or owner_action, or one resumed and not yet dispatched, can be reverified.")
    base = st.get("base_sha")
    if not base or commits_since(base, excluded_commits(state, task_id, base)) == 0:
        return (f"reverify: {task_id} has no commit of its own since its base (owner and foreign "
                "commits excluded): there is no work to verify. Use resume.")
    if not last_claim(state, task_id):
        return (f"reverify: no claim of {task_id} with a summary is kept (state.json last_claim, or "
                "harness/claim.json); use resume.")
    return ""


def fix_commits(state: dict, task_id: str) -> list[str]:
    """Commits made after the task's last own commit that are no task's own: the owner's fix."""
    st = state["tasks"][task_id]
    own = st.get("own_commits") or []
    since = own[-1] if own else st["base_sha"]
    every_own = {c for s in state["tasks"].values() for c in s.get("own_commits", [])}
    return [c for c in git("rev-list", "--reverse", f"{since}..HEAD").splitlines() if c not in every_own]


def reverify_claim(state: dict, task_id: str, kept: dict) -> dict:
    """The claim of a reverify: the last one recorded, as a claim for review, with the owner's note."""
    fixes = fix_commits(state, task_id)
    note = "reverified by owner after " + (", ".join(c[:10] for c in fixes) if fixes else "no new commit")
    notes = "\n".join(x for x in (str(kept.get("notes_for_owner") or "").strip(), note) if x)
    return {"task_id": task_id, "status": "review", "summary": kept.get("summary", ""),
            "notes_for_owner": notes}


# --------------------------------------------------------------------------- task lint (F6)


_PATH_TOKEN = re.compile(r"`([^`\s]+/[^`\s]*|[^`\s]+\.[A-Za-z0-9]{1,5})`")
_WRITE_VERBS = re.compile(r"\b(write|writes|create|creates|add|adds|amend|amends|edit|edits|update|"
                          r"updates|modify|modifies|change|changes|fix|fixes|rename|renames)\b", re.I)


def plan_problem(doc: dict) -> str:
    """'' when the owner approved spec, roadmap and the brief of the milestone, else what is missing."""
    return approvals.milestone_problem(REPO, doc.get("milestone") or "?")


def lint_tasks(doc: dict) -> list[str]:
    """Warnings about the task list: paths a task appears to ask its agent to write that are
    outside the agent's write allowlist (the M45-055 block), acceptance checks that repeat a
    global check (F8), and a milestone planned without the owner's approved spec, roadmap and brief."""
    roles = pathrules.load_roles(REPO)
    warnings = []
    missing = plan_problem(doc)
    if missing:
        warnings.append(f"milestone {doc.get('milestone')}: planned without approval: {missing}")
    globals_ = [json.dumps(g, sort_keys=True) for g in C.GLOBAL_CHECKS]
    for t in doc["tasks"]:
        if t["agent"] == GATE:
            continue
        cfg = roles.get(t["agent"], {})
        for sentence in re.split(r"(?<=[.;:])\s+", t.get("description", "")):
            if not _WRITE_VERBS.search(sentence):
                continue
            for path in _PATH_TOKEN.findall(sentence):
                path = path.strip("./")
                if "*" in path or path.startswith(("http", "-")):
                    continue
                if not matches_allow(cfg, path):
                    warnings.append(f"{t['id']}: '{path}' looks like a file to write, outside the "
                                    f"write allowlist of '{t['agent']}'")
        for check in t.get("acceptance", []):
            if json.dumps(check, sort_keys=True) in globals_:
                warnings.append(f"{t['id']}: acceptance check repeats a global check: {json.dumps(check)}")
    return sorted(set(warnings))


def matches_allow(cfg: dict, path: str) -> bool:
    return (pathrules.matches_any(path, cfg.get("allow_write", []))
            and not pathrules.matches_any(path, cfg.get("deny_write", [])))
