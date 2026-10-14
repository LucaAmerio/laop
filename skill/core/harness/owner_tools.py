"""Owner utilities for the loop state (core file: do not edit).

Usage (from the repository root, in a terminal or from an owner session):

    python harness/owner_tools.py                       status and this help
    python harness/owner_tools.py <action> key=value ...

Actions:

status          summary of the loop state, the bound session and the queued owner actions
pass_gate       task=<gate id>          pass a gate (a "final" gate refuses while provisional
                                        decisions are open)
unblock         task=<id>               back to todo, attempts reset, fresh base at next dispatch
resume          task=<id>               back to todo, base and attempts kept
ratify          qid=<question key>      accept a provisional decision of the resolver
override        qid=<question key> [note="..."]
                                        reject it: the task that resumed with it and every task
                                        built on it since go back to todo, with a steer note
decide          qid=<question> verdict=agree|amend|differ [note="..."] [obvious=yes]
                                        decide a question that waits with the recommendation of
                                        resolver and challenger: agree applies it, amend applies it
                                        with your note, differ applies your note instead; the task
                                        resumes. obvious=yes: the resolver could have decided it
trust           category=<R or D id> level=owner|warmup|auto
                                        set how questions of a category are decided: by you, by you
                                        until the warm-up is passed, or provisionally by the resolver
owner_commit    commit=<sha>            an owner commit made while a task was dispatched: excluded
                                        from every task's range
foreign_commit  task=<id> commit=<sha>  a commit excluded for that task only
allow_path      task=<id> path=<rel>    a path outside the role's allowlist accepted for that task
clear_halt                              clear a halt so that the loop can run again
release                                 clear the orchestrator binding (a session crashed while
                                        running the loop)
lint                                    lint harness/tasks.json (paths outside allowlists,
                                        acceptance checks that repeat a global check)
approve         doc=spec|roadmap|<path>  approve the spec, the roadmap or a milestone brief: records its
                brief=<milestone>        digest in harness/approvals.json (an edited document needs a new
                                        approval); refused while it has [NEEDS CLARIFICATION] markers
approvals       [milestone=<m>]         approval status of spec, roadmap and briefs
archive                                 move state.json to harness/logs (start of a new milestone);
                                        the resolver trust ledger is carried over

While a task is in flight, state-changing actions are queued and applied by the next step
script, so that a long step never overwrites them; otherwise they are applied at once.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import approvals  # noqa: E402
import loop_core as L  # noqa: E402
import statefile  # noqa: E402


def parse(argv: list[str]) -> dict:
    entry: dict = {"action": argv[0] if argv else "status"}
    for arg in argv[1:]:
        key, sep, value = arg.partition("=")
        if not sep:
            raise SystemExit(f"Arguments are key=value, got {arg!r}. Run without arguments for help.")
        entry[{"task": "task_id"}.get(key, key)] = value
    return entry


def status() -> str:
    doc = L.load_tasks()
    state = L.load_state(doc)
    L.write_status(doc, state)
    queue = statefile.read(L.QUEUE_FILE).get("actions", [])
    lines = [L.STATUS_FILE.read_text(encoding="utf-8"),
             f"Orchestrator session: {state.get('orchestrator_session') or 'none'}",
             f"Queued owner actions: {len(queue)}"]
    return "\n".join(lines)


def run(entry: dict) -> str:
    action = entry["action"]
    if action == "status":
        return status() + "\n\n" + __doc__
    if action == "lint":
        warnings = L.lint_tasks(L.load_tasks())
        return "\n".join(warnings) or "No warnings."
    if action == "approve":
        rel = (approvals.brief_rel(entry["brief"]) if entry.get("brief")
               else approvals.NAMES.get(entry.get("doc", ""), entry.get("doc", "")))
        if not rel:
            return "approve needs doc=spec|roadmap|<path> or brief=<milestone>."
        try:
            return approvals.approve(L.REPO, rel)
        except approvals.ApprovalError as exc:
            return f"Not approved: {exc}"
    if action == "approvals":
        milestone = entry.get("milestone") or (L.load_json(L.TASKS_FILE) or {}).get("milestone")
        return approvals.report(L.REPO, milestone)
    if action == "archive":
        doc = L.load_tasks()
        target = L.LOG_DIR / f"state_{doc.get('milestone', 'unknown')}_{dt.datetime.now():%Y%m%d_%H%M}.json"
        L.LOG_DIR.mkdir(parents=True, exist_ok=True)
        with statefile.locked(L.STATE_FILE):
            ledger = statefile.read(L.STATE_FILE).get("ledger")
            L.STATE_FILE.rename(target)
            if ledger:  # the trust earned by the resolver outlives the milestone
                statefile.write(L.STATE_FILE, {"ledger": ledger})
        return f"state archived to {target}" + (" (resolver trust ledger kept)" if ledger else "")
    if action not in L.OWNER_ACTIONS:
        return f"Unknown action {action!r}. Run without arguments for help."
    if action == "release":
        # orchestrator_session belongs to the hooks: cleared directly, under the state lock
        statefile.update(L.STATE_FILE, lambda s: s.update(orchestrator_session=None))
    if action == "clear_halt":
        statefile.update(L.STATE_FILE, lambda s: s.update(stop_guard={}))

    L.enqueue_owner_action(entry)
    in_flight = bool(statefile.read(L.STATE_FILE).get("current"))
    if in_flight:
        return (f"{action} queued: a task is in flight; the next step script applies it. "
                "Run 'status' to check.")
    try:
        with L.step() as (doc, state, _digest):
            pass  # step() applied the queue and saved the state
    except L.LoopError as exc:
        return f"{action} queued, not applied yet ({exc})."
    return f"{action} applied.\n" + "\n".join(state["events"][-3:])


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    print(run(parse(sys.argv[1:])))
