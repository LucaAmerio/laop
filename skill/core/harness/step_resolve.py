"""Loop step 4: record the resolver's resolution and the challenger's challenge (core file).

Run by the orchestrator as: python harness/step_resolve.py

The resolver writes harness/resolutions/<task>_<question>_<n>.md: first line ``RESOLUTION: ANSWER``
or ``RESOLUTION: ESCALATE``, a ``Category:`` line (an R or D id of the principles file) and, in
both cases, its recommended decision. The challenger then always appends a section containing a
line ``CHALLENGE: AGREE``, ``AMEND``, ``COUNTER`` or ``ESCALATE`` (the last such line it appends
counts), so that every question reaches the owner with a recommendation that was attacked.

Who decides (``resolver.mode`` of harness/project.json and the trust ledger of the state):

- the question is decided provisionally, and the task resumes at once, only when the mode is
  ``auto``, the resolver answered, the challenger agreed or amended (same decision), and the
  question's category is autonomous in the ledger;
- otherwise the task is blocked for the owner, with the recommendation: the owner answers with
  ``owner_tools.py decide`` (agree, amend or differ), which resumes the task and feeds the ledger.
  A delegated category becomes autonomous after ``warmup`` accepted answers in a row; an override
  of a provisional decision sends it back.
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import loop_config as C  # noqa: E402
import loop_core as L  # noqa: E402

NEXT = "NEXT ACTION: run python harness/step_next.py"


def _first_line(text: str, pattern: str) -> str | None:
    first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    m = re.fullmatch(pattern, first)
    return m.group(1) if m else None


def _retry_or_block(doc: dict, state: dict, task: dict, who: str, expected: str) -> str:
    current = state["current"]
    current["resolve_tries"] = current.get("resolve_tries", 0) + 1
    blocked = L.guard_blocks_note(who, current["resolve_time"])
    if current["resolve_tries"] >= 2:
        return _for_owner(doc, state, task, f"the {who} did not write a valid '{expected}' line "
                          f"twice.{blocked}", with_recommendation=False)
    L.save_state(doc, state)
    return (f"NO VALID {who.upper()} OUTPUT in {current['resolution']} (expected a line '{expected}')."
            f"{blocked}\n" + L.spawn_instruction(state, "step_resolve"))


def _for_owner(doc: dict, state: dict, task: dict, why: str, with_recommendation: bool = True) -> str:
    """Block the task for the owner; with a recommendation, register the question for 'decide'."""
    current = state["current"]
    claim = current.get("claim") or {}
    question = claim.get("question") or claim.get("summary", "")
    qid = current["question_id"]
    reason = f"{qid}: {question} [{why}; see {current['resolution']}]"
    if with_recommendation:
        state["questions"][qid] = {"task": task["id"], "file": current["resolution"], "status": "open",
                                   "category": current.get("category", ""), "why": why, "time": L.now()}
        reason += (f" Decide: python harness/owner_tools.py decide qid={qid} "
                   "verdict=agree|amend|differ [note=\"...\"] [obvious=yes]")
    L.block(state, task, reason, "question")
    state["current"] = None
    L.save_state(doc, state)
    return f"FOR THE OWNER: {task['id']} waits for the owner on {qid} ({why}).\n{NEXT}"


def run(doc: dict, state: dict) -> str:
    refused = L.refuse_without_guard(doc, state, "step_resolve")
    if refused:
        return refused
    current = state["current"]
    if not current or current["phase"] not in ("resolving", "challenging"):
        return f"NOTHING TO RESOLVE.\n{NEXT}"
    task = L.task_by_id(doc, current["task_id"])
    # Backstop: the resolver roles write only their resolution file (git-ignored).
    owner_files = L.owner_paths()
    untracked = set(L.untracked_files()) - owner_files
    tracked = [f for f in L.dirty_files() if f not in untracked and f not in owner_files]
    if L.head_sha() != current.get("resolve_head") or tracked:
        return L.halt(doc, state, f"{task['id']}: the resolver or the challenger changed the repository "
                                  f"(new commits or modified files: {tracked[:5]})")
    if untracked:
        where = L.quarantine(sorted(untracked), f"{task['id']}_resolution")
        L.event(state, f"{len(untracked)} untracked file(s) left by the resolver moved to "
                       f"{where.relative_to(L.REPO).as_posix()}")
    path = L.REPO / current["resolution"]
    text = path.read_text(encoding="utf-8") if path.exists() else ""

    if current["phase"] == "resolving":
        fresh = path.exists() and path.stat().st_mtime >= current["resolve_time"]
        verdict = _first_line(text, r"RESOLUTION:\s*(ANSWER|ESCALATE)") if fresh else None
        if verdict is None:
            return _retry_or_block(doc, state, task, "resolver", "RESOLUTION: ANSWER | ESCALATE")
        category = L.question_category(text)
        token_line = L.spawn(state, L.CHALLENGER, L.CHALLENGE_PROMPT_FILE, "CHALLENGE", task["id"])
        # only what the challenger appends counts: a CHALLENGE line in the resolver's text is ignored
        current.update(phase="challenging", challenge_time=time.time(), resolve_tries=0,
                       resolution_len=len(text), resolver_verdict=verdict, category=category)
        L.CHALLENGE_PROMPT_FILE.write_text(
            L.build_challenge_prompt(task, current.get("claim") or {}, token_line, current["resolution"],
                                     escalated=verdict == "ESCALATE"),
            encoding="utf-8")
        L.event(state, f"{task['id']}: resolver {verdict.lower()} on {current['question_id']} "
                       f"(category {category or 'unknown'}); challenge requested")
        L.save_state(doc, state)
        return (f"CHALLENGE: the resolver wrote its {verdict.lower()} on {current['question_id']}; the "
                "challenger checks it.\n" + L.spawn_instruction(state, "step_resolve"))

    # challenging: the last CHALLENGE line the challenger appended
    fresh = path.exists() and path.stat().st_mtime >= current["challenge_time"]
    appended = text[current.get("resolution_len", 0):]
    found = re.findall(r"(?m)^\s*CHALLENGE:\s*(AGREE|AMEND|COUNTER|ESCALATE)\s*$", appended)
    verdict = found[-1] if fresh and found else None
    if verdict is None:
        return _retry_or_block(doc, state, task, "challenger", "CHALLENGE: AGREE | AMEND | COUNTER | ESCALATE")

    qid, category = current["question_id"], current.get("category", "")
    st = state["tasks"][task["id"]]
    st.setdefault("resolutions", []).append(current["resolution"])
    state["resolutions_this_run"] = state.get("resolutions_this_run", 0) + 1
    agreed = current.get("resolver_verdict") == "ANSWER" and verdict in ("AGREE", "AMEND")
    status = L.category_status(state, category)
    if not agreed:
        who = ("the resolver escalated" if current.get("resolver_verdict") == "ESCALATE"
               else f"the challenger answered {verdict}")
        return _for_owner(doc, state, task, f"{who}; recommendation prepared")
    if C.RESOLVER_MODE != "auto" or status != "auto":
        if C.RESOLVER_MODE != "auto":
            why = "mode advise: the owner decides every question"
        elif status == "warmup":
            why = (f"category {category} in warm-up ({L.ledger_entry(state, category)['streak']}/"
                   f"{C.RESOLVER_WARMUP} accepted)")
        else:
            why = f"category {category or 'unknown'} is the owner's"
        return _for_owner(doc, state, task, f"resolver and challenger agree; {why}")

    st["pending_resolution"] = current["resolution"]
    st["pending_resolution_by"] = "provisional"
    st["status"] = "todo"
    key = qid if qid not in state["provisional"] else f"{qid}@{task['id']}#{len(st['resolutions'])}"
    state["provisional"][key] = {"task": task["id"], "file": current["resolution"], "status": "open",
                                 "category": category, "time": L.now()}
    how = "with the challenger's amendments" if verdict == "AMEND" else "challenger agrees"
    L.event(state, f"{task['id']}: {qid} answered provisionally ({how}, category {category}); task resumes")
    L.notify(f"{qid} answered provisionally", f"Task {task['id']} resumes. Ratify or override: "
             f"{current['resolution']}", level="all")
    state["current"] = None
    L.save_state(doc, state)
    return f"RESOLVED: {qid} answered provisionally; {task['id']} goes back to the queue.\n{NEXT}"


def step() -> str:
    with L.step() as (doc, state, _digest):
        return run(doc, state)


if __name__ == "__main__":
    try:
        print(step())
    except (L.LoopError, C.ConfigError) as exc:
        print(f"HALTED: configuration problem: {exc}\nNEXT ACTION: tell the owner and end your turn.")
