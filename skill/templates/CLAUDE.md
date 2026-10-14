# CLAUDE.md: persistent rules for {{NAME}}

{{PURPOSE}}

{{OWNER_PROFILE}} Treat the owner as a senior reviewer: be precise, flag doubts explicitly, never hide uncertainty.

**North star.** {{NORTH_STAR}} The owner's principles, and what the owner decides personally, are in `docs/owner_principles.md`.

This file belongs to the owner. Agents never edit it; they propose changes in the `notes_for_owner` field of their claim.

## Where things are

- `docs/spec.md`: what to build. Read the sections your task references.
- `docs/plan/roadmap.md`: the milestones; `docs/plan/<milestone>.brief.md`: what the current milestone means to the owner (in and out of scope, exit criteria). Approved by the owner: never edit them.
- `docs/owner_principles.md`: the owner's principles and decision rules.
- `docs/adr/`: design decisions. `{{OPEN_QUESTIONS}}`: questions for the owner.
- `harness/README.md`: how the task loop works. `harness/tasks.json`: tasks of the current milestone. `harness/PROGRESS.md`: session log.
{{INTERPRETER_LINE}}

## Non-negotiable rules

1. Never guess a convention, a default behaviour or a design choice the documents do not settle. Append an entry to `{{OPEN_QUESTIONS}}` (question, options, recommendation, consequences), claim `blocked` with `block_type: "question"` and the entry's id as `question_id`, and stop.
2. Data the project uses as ground truth (references, fixtures, tolerances, locked directories) are frozen: only their owning role writes them, never after the owner locks them, and a tolerance is never loosened to make something pass.
3. If the code disagrees with a reference, assume the code is wrong until proven otherwise. Never change a reference to match the code.
4. A guard hook enforces your role's permissions. If it blocks an action, do not work around it (no redirection, no script writing the file, no renames): claim `blocked` (`block_type: "harness"`) and explain.
5. No dependency changes: no installs, no edits to dependency files. Request them in your claim.
6. Git: commit only. No push, reset, rebase, checkout, switch, restore, stash or clean. Stage only the files you changed (`git add <path>`), never `git add -A`, `git add .` or `git commit -a`: the owner may be editing other files in the same working tree.
7. One task per session. Do not start another task, even a small one.
8. Create and modify files only with the Write and Edit tools, never through shell commands (redirection, Set-Content, sed -i...).
9. Subagents do not spawn other subagents. Only the orchestrator dispatches work, through the harness scripts.

## Code conventions

{{CONVENTIONS}}

## Session routine

Start: read this file, the tail of `harness/PROGRESS.md`, `git log --oneline -15`, your task in the prompt, and any findings from a rejected attempt.

End:
1. Run your task's acceptance checks and the global checks listed in your prompt.
2. Commit with a message starting with the task id (`M1-004: ...`). Leave the working tree clean.
3. Append a short entry to `harness/PROGRESS.md` (done, deliberately left out, to review) and commit it.
4. Write `harness/claim.json` (not committed):

```json
{
  "task_id": "M1-004",
  "status": "review",
  "summary": "what was done and what the tests prove",
  "question": "only when blocked: the precise question for the owner",
  "question_id": "only when blocked on an open question: its id, e.g. Q12",
  "block_type": "only when blocked: question | owner_action | harness",
  "notes_for_owner": "assumptions, doubts, suggested changes to CLAUDE.md or the spec"
}
```

`review` means you believe the task is complete. The harness verifies it independently; you never mark a task done. `block_type`: `question` for an open question (it may be answered on the owner's behalf by the resolver, provisionally), `owner_action` for something only the owner can do (pass a gate, provide a licence or a data file), `harness` when the harness itself prevents the work.

5. Your final reply is at most 5 lines: task id, claim status, one-line summary.
