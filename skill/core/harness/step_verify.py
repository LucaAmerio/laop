"""Loop step 2: verify the work of the subagent that just returned (core file: do not edit).

Run by the orchestrator as: python harness/step_verify.py
Checks integrity, protected paths and the claim, then runs the global and acceptance checks.
Asks for a review, or, for a question the resolver may answer, for a resolution.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import loop_config as C  # noqa: E402
import loop_core as L  # noqa: E402
import pathrules  # noqa: E402

NEXT = "NEXT ACTION: run python harness/step_next.py"


def run(doc: dict, state: dict, digest_at_load: str) -> str:
    refused = L.refuse_without_guard(doc, state, "step_verify")
    if refused:
        return refused
    current = state["current"]
    if not current or current["phase"] != "dispatched":
        return f"NOTHING TO VERIFY.\n{NEXT}"
    task = L.task_by_id(doc, current["task_id"])
    st = state["tasks"][task["id"]]
    base = st["base_sha"]

    # Git-ignored files the task must not touch: the loop state, verdicts, resolutions and the
    # integrity paths of the config (F4). git does not see them, so they are compared by digest.
    changed = L.integrity_changes(current.get("integrity", {}), digest_at_load)
    if changed:
        st["status"] = "blocked"
        st["block_type"] = "harness"
        st["blocked_reason"] = "integrity: changed during the task: " + ", ".join(changed)
        state["current"] = None
        return L.halt(doc, state, f"{task['id']}: files the task must not touch changed while it ran "
                                  f"(git-ignored, so the protected-path check cannot see them): "
                                  f"{', '.join(changed)}")

    # Files the owner edited in this working tree while the task ran (recorded by the guard): not
    # the task's work, so they are left out of every check below; a commit of the dispatch that
    # touches only such files is the owner's. The record is pruned after the checks.
    owner_files = L.owner_paths()
    if current.get("dispatch_sha"):
        L.record_own_commits(state, task["id"], current["dispatch_sha"])
        moved = L.claim_owner_commits(state, task["id"], owner_files)
        if moved:
            L.event(state, f"{task['id']}: commit(s) of owner files only recorded as owner commits: {moved}")
    excluded = L.excluded_commits(state, task["id"], base)
    if owner_files:
        L.event(state, f"{task['id']}: owner edits in the working tree ignored by the checks: "
                       f"{sorted(owner_files)[:5]}")

    def task_dirty() -> list[str]:
        return [f for f in L.dirty_files() if f not in owner_files]

    # Stray untracked files outside the allowlist (scratch output the subagent cannot delete) are
    # moved aside instead of halting. Committed or tracked changes still halt.
    strays = [p for p in L.untracked_files()
              if p not in owner_files and not pathrules.check_write(task["agent"], p, L.REPO)[0]]
    if strays:
        where = L.quarantine(strays, f"{task['id']}_attempt{current['attempt']}")
        L.event(state, f"{task['id']}: {len(strays)} untracked file(s) outside the allowlist moved to "
                       f"{where.relative_to(L.REPO).as_posix()}: {sorted(strays)[:5]}")

    violations = []
    allowed_once = set(st.get("allowed_paths", []))  # owner_tools.py, action allow_path
    for path in L.changed_since(base, excluded):
        if path in allowed_once or path in owner_files:
            continue
        ok, why = pathrules.check_write(task["agent"], path, L.REPO)
        if not ok:
            violations.append(why)
    if violations:
        st.update(status="blocked", block_type="harness",
                  blocked_reason="protected paths modified: " + "; ".join(violations[:5]))
        state["current"] = None
        return L.halt(doc, state, f"{task['id']} modified protected paths: " + "; ".join(violations[:10]))

    if (L.file_hash(L.OPEN_QUESTIONS) != current.get("open_questions_hash")
            and C.OPEN_QUESTIONS_REL not in owner_files):
        L.event(state, f"{task['id']} changed {C.OPEN_QUESTIONS_REL}")

    claim = L.load_json(L.CLAIM_FILE) if L.CLAIM_FILE.exists() else None
    valid = isinstance(claim, dict) and claim.get("task_id") == task["id"] \
        and claim.get("status") in ("review", "blocked")

    # An empty run: nothing committed by this dispatch (counted from dispatch_sha, not base_sha,
    # which also holds the commits of earlier attempts) and nothing left uncommitted. A run cut
    # short by an API error (a StopFailure recorded after the dispatch) is not an attempt either,
    # even with commits. Both count towards max_empty_runs instead.
    produced = L.commits_since(current.get("dispatch_sha") or base, excluded) > 0 or bool(task_dirty())
    interrupted = (L.stop_failure_since(current["dispatch_time"])
                   if not valid and current.get("dispatch_time") else "")
    if not valid and (not produced or interrupted):
        state["empty_runs"] += 1
        state["current"] = None
        st["status"] = "todo"
        if L.commits_since(base, excluded) == 0 and not task_dirty():
            st.pop("base_sha", None)  # no attempt committed anything: a fresh base at the next dispatch
        what = f"was interrupted by an API error ({interrupted})" if interrupted else "produced nothing"
        if interrupted:
            st["interrupted"] = interrupted
        if state["empty_runs"] >= C.MAX_EMPTY_RUNS:
            return L.halt(doc, state, f"{state['empty_runs']} dispatches in a row ended without a claim, the "
                                      f"last one {what} (usage limit reached, or the subagent was not spawned?)")
        L.event(state, f"{task['id']}: the subagent {what}; dispatched again, not counted as an attempt")
        L.save_state(doc, state)
        return (f"NO RESULT: the subagent {what} and left no claim; the task will be dispatched again "
                f"(not counted as an attempt).\n{NEXT}")
    state["empty_runs"] = 0

    if not valid:
        L.fail_attempt(doc, state, task, "No valid harness/claim.json for this task was written. "
                       "End every session with a claim (see CLAUDE.md).")
    elif claim["status"] == "blocked":
        block_type = claim.get("block_type") if claim.get("block_type") in L.BLOCK_TYPES else "question"
        reason = claim.get("question") or claim.get("summary", "")
        # a resolution needs a clean tree: the resolver phases must not inherit uncommitted work
        if block_type == "question" and L.may_resolve(state, task["id"]) and not task_dirty():
            return request_resolution(doc, state, task, claim)
        L.block(state, task, reason, block_type)
    elif task_dirty():
        L.fail_attempt(doc, state, task, "Uncommitted changes left in the working tree: "
                       f"{task_dirty()[:10]}. Commit your work before claiming review.")
    else:
        ok, report = L.run_checks(task, current["attempt"])
        if not ok:
            L.fail_attempt(doc, state, task, "Harness checks failed.\n\n" + report)
        elif task.get("review", True):
            return request_review(doc, state, task, base, excluded, sorted(owner_files))
        else:
            L.mark_done(state, task, "checks passed, review not required")
    outcome = st["status"]
    state["current"] = None
    L.save_state(doc, state)
    return f"RECORDED: {task['id']} is now '{outcome}'.\n{NEXT}"


def request_review(doc: dict, state: dict, task: dict, base: str, excluded: list[str],
                   owner_files: list[str] | None = None) -> str:
    current = state["current"]
    L.VERDICT_DIR.mkdir(parents=True, exist_ok=True)
    verdict = L.VERDICT_DIR / f"{task['id']}_attempt{current['attempt']}.md"
    verdict.unlink(missing_ok=True)
    verdict_rel = verdict.relative_to(L.REPO).as_posix()
    reviewer = task.get("reviewer", "evaluator")
    token_line = L.spawn(state, reviewer, L.REVIEW_PROMPT_FILE, "REVIEW", task["id"])
    current.update(phase="awaiting_review", verdict=verdict_rel, verify_time=time.time(), review_tries=0)
    current.pop("integrity", None)
    L.REVIEW_PROMPT_FILE.write_text(L.build_review_prompt(task, base, verdict_rel, token_line, excluded,
                                                          owner_files or []),
                                    encoding="utf-8")
    L.event(state, f"{task['id']} checks passed, review requested")
    L.save_state(doc, state)
    return (f"REVIEW: checks passed for {task['id']}.\n" + L.spawn_instruction(state, "step_record"))


def request_resolution(doc: dict, state: dict, task: dict, claim: dict) -> str:
    current = state["current"]
    st = state["tasks"][task["id"]]
    qid = str(claim.get("question_id") or "Q").replace("/", "_")
    L.RESOLUTION_DIR.mkdir(parents=True, exist_ok=True)
    out = L.RESOLUTION_DIR / f"{task['id']}_{qid}_{len(st.get('resolutions', [])) + 1}.md"
    out.unlink(missing_ok=True)
    out_rel = out.relative_to(L.REPO).as_posix()
    token_line = L.spawn(state, L.RESOLVER, L.RESOLVE_PROMPT_FILE, "RESOLVE", task["id"])
    current.update(phase="resolving", question_id=qid, resolution=out_rel, resolve_time=time.time(),
                   resolve_tries=0, claim=claim, resolve_head=L.head_sha())
    current.pop("integrity", None)
    L.RESOLVE_PROMPT_FILE.write_text(L.build_resolve_prompt(task, claim, token_line, out_rel), encoding="utf-8")
    L.event(state, f"{task['id']} asked {qid}; resolution requested")
    L.save_state(doc, state)
    return (f"RESOLVE: {task['id']} stopped on question {qid}; the resolver decides whether it needs the "
            "owner.\n" + L.spawn_instruction(state, "step_resolve"))


def step() -> str:
    with L.step() as (doc, state, digest):
        return run(doc, state, digest)


if __name__ == "__main__":
    try:
        print(step())
    except (L.LoopError, C.ConfigError) as exc:
        print(f"HALTED: configuration problem: {exc}\nNEXT ACTION: tell the owner and end your turn.")
