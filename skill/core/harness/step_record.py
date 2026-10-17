"""Loop step 3: record the reviewer's verdict (core file: do not edit).

Run by the orchestrator as: python harness/step_record.py
The reviewer writes its review to harness/verdicts/<task>_attempt<n>.md; the first line
decides: VERDICT: PASS or VERDICT: NEEDS_WORK.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import loop_config as C  # noqa: E402
import loop_core as L  # noqa: E402

NEXT = "NEXT ACTION: run python harness/step_next.py"


def run(doc: dict, state: dict) -> str:
    refused = L.refuse_without_guard(doc, state, "step_record")
    if refused:
        return refused
    current = state["current"]
    if not current or current["phase"] != "awaiting_review":
        return f"NOTHING TO RECORD.\n{NEXT}"
    task = L.task_by_id(doc, current["task_id"])
    verdict_path = L.REPO / current["verdict"]

    text = ""
    if verdict_path.exists() and verdict_path.stat().st_mtime >= current["verify_time"]:
        text = verdict_path.read_text(encoding="utf-8")
    first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    match = re.fullmatch(r"VERDICT:\s*(PASS|NEEDS_WORK)", first)

    if not match:
        current["review_tries"] = current.get("review_tries", 0) + 1
        # a verdict the reviewer tried to write and the guard refused is not a missing verdict
        blocked = L.guard_blocks_note(current.get("reviewer", "evaluator"), current["verify_time"])
        if current["review_tries"] >= 2:
            return L.halt(doc, state, f"the reviewer did not produce a valid verdict for {task['id']} "
                                      f"twice; the work is committed, restart /{C.LOOP_COMMAND} to re-review."
                                      + blocked)
        L.save_state(doc, state)
        return (f"NO VALID VERDICT in {current['verdict']} (the first line must be 'VERDICT: PASS' or "
                f"'VERDICT: NEEDS_WORK').{blocked}\n" + L.spawn_instruction(state, "step_record"))

    if match.group(1) == "PASS":
        L.mark_done(state, task, f"{current.get('reviewer', 'evaluator')} PASS", notes=text)
    else:
        L.fail_attempt(doc, state, task, "Reviewer verdict: NEEDS_WORK.\n\n" + text, kind="review")
    outcome = state["tasks"][task["id"]]["status"]
    state["current"] = None
    L.save_state(doc, state)
    return f"RECORDED: {task['id']} is now '{outcome}'.\n{NEXT}"


def step() -> str:
    with L.step() as (doc, state, _digest):
        return run(doc, state)


if __name__ == "__main__":
    try:
        print(step())
    except (L.LoopError, C.ConfigError) as exc:
        print(f"HALTED: configuration problem: {exc}\nNEXT ACTION: tell the owner and end your turn.")
