"""Loop step 1: pick the next task and prepare the subagent prompt (core file: do not edit).

Run by the orchestrator as: python harness/step_next.py
The guard binds the calling session as the orchestrator just before this script runs.
Prints what happened and a single NEXT ACTION line.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import loop_config as C  # noqa: E402
import loop_core as L  # noqa: E402

END = "NEXT ACTION: tell the owner and end your turn."


def run(doc: dict, state: dict) -> str:
    refused = L.refuse_without_guard(doc, state, "step_next")
    if refused:
        return refused

    if L.STOP_FILE.exists():
        state["loop_active"] = False
        L.save_state(doc, state)
        return "STOPPED: AGENT_STOP is present at the repository root.\n" + END

    if not state.get("orchestrator_session"):
        return (f"REFUSED: no session is bound as orchestrator. Start the loop with /{C.LOOP_COMMAND} "
                "in Claude Code: the guard binds the session that runs this script.\n" + END)

    branch = L.current_branch()
    if branch in C.PROTECTED_BRANCHES or branch == "HEAD":
        return (f"REFUSED: the loop does not run on '{branch}'. The owner creates the milestone "
                "branch (for example 'git switch -c m1') and starts the loop again.\n" + END)

    if state["halted"]:
        return L.terminal(f"HALTED: {state['halted']}\n{END}", doc, state)

    current = state["current"]
    if current:
        phase = current["phase"]
        if phase == "dispatched":
            return (f"PENDING: task {current['task_id']} was dispatched to '{current['agent']}' "
                    "but not verified yet. If the subagent never ran or left nothing, step_verify "
                    "records an empty run and the task is dispatched again: no attempt is counted.\n"
                    "NEXT ACTION: run python harness/step_verify.py")
        then = {"awaiting_review": "step_record", "resolving": "step_resolve",
                "challenging": "step_resolve"}[phase]
        return (f"PENDING: task {current['task_id']} is waiting for its {phase.replace('_', ' ')}.\n"
                + L.spawn_instruction(state, then))

    # Checked only here, with no task in progress: the soft stop never interrupts a task.
    if L.SOFT_STOP_FILE.exists():
        state["loop_active"] = False
        L.event(state, "soft stop: no new task dispatched")
        L.save_state(doc, state)
        L.notify("loop soft-stopped", f"Stopped between tasks.\n{L.progress(state)}")
        return L.terminal("STOPPED: AGENT_SOFT_STOP is present at the repository root; the last task "
                          "is finished and no new task was dispatched. Delete the file to resume.\n"
                          + END, doc, state)

    starting = not state["loop_active"]
    if starting and C.REQUIRE_BRIEF and L.plan_problem(doc):
        return (f"REFUSED: the milestone {doc.get('milestone')} has no approved definition: "
                f"{L.plan_problem(doc)}. The owner approves spec, roadmap and brief before the loop "
                "starts (harness/README.md, 'Before the plan').\n" + END)
    warnings = L.lint_tasks(doc) if starting else []
    if starting and C.RESOLVER_ENABLED and L.principles_problem():
        warnings.append(f"resolver mode {C.RESOLVER_MODE} but {L.principles_problem()}: every question "
                        "goes to the owner without a recommendation until it is fixed")
    # No task in flight: forget the owner's edits that were committed or reverted meanwhile.
    owner_files = L.prune_owner_paths()

    # A gate never stops the loop by itself: the loop runs every task that does not depend on it
    # and stops only when nothing else can run.
    gates = L.open_gates(doc, state)
    task = L.next_task(doc, state)
    if task is None:
        state["loop_active"] = False
        state["resolutions_this_run"] = 0
        if gates:
            ids = {g["id"] for g in gates}
            listed = "; ".join(f"{g['id']} ({g['title']})" for g in gates)
            behind = L.waiting_behind(doc, state, ids)
            msg = (f"gates awaiting the owner: {listed}. Tasks waiting behind them: "
                   f"{', '.join(behind) or 'none'}")
            L.event(state, f"loop stopped at gates: {', '.join(sorted(ids))}")
            L.notify("gates for the owner", f"{msg}\n{L.progress(state)}\n{L.provisional_lines(state)}")
            L.save_state(doc, state)
            return L.terminal(f"STOPPED: {msg}. The owner reviews them and passes each with "
                              "harness/owner_tools.py (ACTION 'pass_gate').\n"
                              "NEXT ACTION: tell the owner which gates are waiting and end your turn.",
                              doc, state)
        pending = [t["id"] for t in doc["tasks"] if state["tasks"][t["id"]]["status"] != "done"]
        if pending:
            msg = f"No runnable task. Not done: {', '.join(pending)}"
            L.notify("loop idle", f"{msg}\n{L.provisional_lines(state)}")
        else:
            msg = f"All {len(doc['tasks'])} tasks of {doc.get('milestone')} are done."
            L.notify("milestone complete", f"{msg} Review, merge and push.\n{L.provisional_lines(state)}")
        L.event(state, f"loop finished: {msg}")
        L.save_state(doc, state)
        return L.terminal(f"LOOP FINISHED: {msg}\nNEXT ACTION: report this to the owner in two lines "
                          "and end your turn.", doc, state)

    if starting:
        state["iterations"] = 0  # the cap counts the dispatches of this run, not of the milestone
    if state["iterations"] >= C.MAX_ITERATIONS:
        state["loop_active"] = False
        L.notify("iteration cap reached", f"Stopped after {state['iterations']} dispatches.")
        L.save_state(doc, state)
        return L.terminal("LOOP FINISHED: iteration cap reached (project.json, max_iterations).\n"
                          + END, doc, state)

    if state.get("last_task") != task["id"]:
        # Files left uncommitted by an earlier task: untracked ones are moved aside and kept; a
        # modified tracked file still halts.
        # The owner's own pending edits (recorded by the guard) are left alone.
        untracked = set(L.untracked_files()) - owner_files
        tracked = [f for f in L.dirty_files() if f not in untracked and f not in owner_files]
        if tracked:
            return L.halt(doc, state, f"working tree not clean before {task['id']}: {tracked[:10]}")
        if untracked:
            where = L.quarantine(sorted(untracked), f"before_{task['id']}")
            L.event(state, f"{len(untracked)} untracked file(s) left by {state.get('last_task')} moved "
                           f"to {where.relative_to(L.REPO).as_posix()}: {sorted(untracked)[:5]}")

    st = state["tasks"][task["id"]]
    if "base_sha" not in st:
        # A fresh base: from here on the task's own commits are tracked dispatch by dispatch.
        st.update(base_sha=L.head_sha(), own_commits=[], tracked=True)
    st["status"] = "in_progress"
    L.CLAIM_FILE.unlink(missing_ok=True)
    state["current"] = {
        "task_id": task["id"],
        "agent": task["agent"],
        "reviewer": task.get("reviewer", "evaluator"),
        "phase": "dispatched",
        "attempt": st["attempts"] + 1,
        "dispatch_sha": L.head_sha(),
        "dispatch_time": time.time(),
        "open_questions_hash": L.file_hash(L.OPEN_QUESTIONS),
    }
    token_line = L.spawn(state, task["agent"], L.PROMPT_FILE, "DISPATCH", task["id"])
    state["last_task"] = task["id"]
    if starting:
        L.notify("loop started", f"First task: {task['id']} ({task['title']}).\n{L.progress(state)}",
                 level="all")
    state["loop_active"] = True
    state["iterations"] += 1
    steer = L.consume_steer(task["id"])
    resolution, resolution_by = "", st.pop("pending_resolution_by", "provisional")
    if st.get("pending_resolution"):
        resolution = L.resolution_text(L.REPO / st.pop("pending_resolution"))
    L.PROMPT_FILE.write_text(L.build_prompt(doc, task, token_line, steer, resolution, resolution_by,
                                            interrupted=st.pop("interrupted", "")),
                             encoding="utf-8")
    L.event(state, f"dispatch {task['id']} to {task['agent']} (attempt {st['attempts'] + 1})")
    # Last change before saving: the integrity snapshot of what the task must not touch (F4).
    state["current"]["integrity"] = L.integrity_snapshot(state)
    L.save_state(doc, state)
    out = (f"DISPATCH: task {task['id']} ({task['title']}) to the '{task['agent']}' subagent.\n"
           + L.spawn_instruction(state, "step_verify"))
    if warnings:
        out = "WARNING (task lint): " + "; ".join(warnings[:10]) + "\n" + out
    return L.terminal(out, doc, state) if starting else out


def step() -> str:
    with L.step() as (doc, state, _digest):
        return run(doc, state)


if __name__ == "__main__":
    try:
        print(step())
    except (L.LoopError, C.ConfigError) as exc:
        print(f"HALTED: configuration problem: {exc}\nNEXT ACTION: tell the owner and end your turn.")
