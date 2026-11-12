# Task-loop harness (laop)

Installed and upgraded by the `laop` skill. The files marked **core** below are identical in every
project and replaced by `/laop upgrade`: do not edit them (the session start check reports a
modified core file). The project's own settings are in the **config** files.

The owner decides what is built and passes every gate. Inside one Claude Code session the main
conversation acts as a thin **orchestrator**: it runs the step scripts and spawns the subagents they
ask for. Every decision (which task, pass or fail, retry, resolve, block, halt) is taken by the
scripts, in code.

```
 owner ──► /laop discover (once) ──► docs/spec.md, docs/plan/roadmap.md ──► owner approves
 owner ──► /laop brief <m> (each milestone) ──► docs/plan/<m>.brief.md ──► owner approves
 owner ──► planner subagent ──► harness/tasks.json ──► owner approves
                                                         │
 /laop-start (this session becomes the orchestrator) ◄┘
    │
    ├─ step_next.py      binds the session, picks the next ready task (gates wait for the owner),
    │                    writes current_prompt.md
    ├─ Agent(<task agent>)   fresh context, foreground; ends with commit + harness/claim.json
    ├─ step_verify.py    integrity, protected paths, claim, acceptance + global checks
    │                    (run by checks.py in a process of their own; CHECKS RUNNING → run it again)
    │    ├─ REVIEW  ─► Agent(<reviewer>) ─► step_record.py   PASS → done, NEEDS_WORK → retry
    │    └─ RESOLVE ─► Agent(resolver) ─► step_resolve.py ─► Agent(challenger) ─► step_resolve.py
    │                     ANSWER + AGREE → provisional decision, task resumes; otherwise → owner
    └─ repeat; the Stop hook keeps the orchestrator going while the loop is active
 the owner's next message in that session makes it the owner's again
```

## Files

| File | Class | Purpose |
|---|---|---|
| `harness/loop_config.py`, `loop_core.py`, `checks.py`, `pathrules.py`, `statefile.py`, `step_*.py`, `*_hook.py`, `session_check.py`, `owner_tools.py`, `README.md` | core | Loop logic, rules engine, hooks, owner tools |
| `.claude/hooks/guard.py` | core | PreToolUse guard: kill switch, permissions per role, dispatch control |
| `.claude/agents/resolver.md`, `challenger.md` | core | The resolution roles |
| `.claude/commands/laop-start.md`, the `hooks` block of `.claude/settings.json` | rendered | Generated from the core and `project.json` |
| `harness/core.lock.json` | core | Version and digests of the core and rendered files |
| `harness/approvals.py` | core | Owner approvals of spec, roadmap and briefs |
| `harness/approvals.json` | owner | Digests of the approved documents (written by `owner_tools.py approve`, never by a role) |
| `docs/spec.md`, `docs/plan/roadmap.md`, `docs/plan/<m>.brief.md` | owner | What to build, which milestones, what each means (written by the `analyst`, approved by the owner) |
| `harness/project.json` | config | Name, interpreters, global checks, limits, protected and lockable paths, external programs, resolver switch, ntfy |
| `harness/roles.json` | config | Project roles and their permissions |
| `.claude/agents/<project roles>.md`, `CLAUDE.md` | config | Role instructions, persistent rules |
| `docs/owner_principles.md` | config | The owner's north star, reserved and delegated categories, escalation signals (read by the resolver roles) |
| `harness/tasks.json` | planner, approved by owner | Tasks of the current milestone |
| `harness/PROGRESS.md` | agents | Session log (committed) |
| `harness/state.json`, `STATUS.md`, `claim.json`, `findings/` (one file per task, a section per rejected attempt), `STEER*.md`, `*_prompt.md`, `owner_queue.json`, `verdicts/`, `resolutions/`, `logs/`, `*.lock` | runtime | Git-ignored |

## Roles

The role of a tool call is decided by the guard:

- inside a subagent whose name is a role: that role;
- in a main conversation: `orchestrator` if its `session_id` is the one bound in `state.json`, otherwise `owner`;
- inside any other subagent (Explore, general-purpose): the role of its main conversation.

Core roles, which `roles.json` cannot redefine:

| Role | Writes | Other limits |
|---|---|---|
| owner | everything except the protected paths | |
| orchestrator | nothing | only the step scripts, `git status`, `git log --oneline -N`; spawns only the subagent the scripts expect, in the foreground, with their prompt |
| resolver, challenger | `harness/resolutions/**` | read-only git; they inherit the `deny_read` of the role whose question they handle |

Project roles (`roles.json`): `allow_write` and `deny_write` (globs), `deny_read` (hidden files, for independent roles), `bash_allow` (first word of every shell command), `git_allow` (git subcommands), `"dispatch": true` (the loop may give it tasks), `"reviewer": true` (it reviews tasks). Loop roles may only commit: the guard denies reset, checkout, switch, restore, stash, clean, rebase, push, merge, pull, cherry-pick, revert, branch and tag.

Binding: the guard binds the session that runs `python harness/step_next.py`. It keeps the orchestrator restrictions until the owner types a prompt in it (UserPromptSubmit hook). Every other session is the owner's: the owner can work on a gate in a second session while the loop runs. The guard refuses to bind a second session while the loop is active in another; `owner_tools.py release` clears a binding left by a crashed session.

Backstops: `step_verify.py` re-checks every file changed by the task against the same rules (writes the guard cannot see, e.g. by Python code) and halts on a violation; untracked files outside the allowlist are moved to `harness/logs/quarantine/` instead. It also compares the digests of the git-ignored files the task must not touch (state, verdicts, resolutions, `integrity_paths`) with those taken at dispatch.

## Before the plan

The planner plans a milestone only once the owner has approved three documents, written by the
`analyst` role through the skill (`/laop discover` once, `/laop brief <m>` before each milestone):

- `docs/spec.md`: stories (P1..Pn, each independently testable), requirements (FR-n, in EARS form:
  "When <trigger>, the system shall <response>"), measurable success criteria (SC-n), non-goals,
  constraints, assumptions and a clarification log;
- `docs/plan/roadmap.md`: the milestones with their intent, the FR ids they cover, dependencies,
  exit criteria and what is deferred; every FR is in one milestone or deferred with a reason;
- `docs/plan/<m>.brief.md`: what the milestone means to the owner, in and out of scope, exit
  criteria, what changed since the roadmap, the decisions the owner must take (they become the
  milestone's first gate) and a pre-mortem.

`python harness/owner_tools.py approve doc=spec` (or `doc=roadmap`, `brief=<m>`) records the
document's digest in `harness/approvals.json`; an edited document needs a new approval, and a
document with a `[NEEDS CLARIFICATION: ...]` marker cannot be approved. `owner_tools.py lint` warns
about a milestone planned without approvals; with `"require_brief": true` in `project.json` (the
default of new installs) `step_next.py` refuses to start the loop. What enters which milestone is
reserved to the owner: neither the resolver nor the challenger approves these documents.

## Task file (`harness/tasks.json`)

```json
{
  "milestone": "m1",
  "tasks": [
    {
      "id": "M1-001",
      "title": "Short title",
      "agent": "builder",
      "reviewer": "evaluator",
      "description": "Goal, scope boundary, done criteria, pitfalls.",
      "spec_refs": ["docs/spec.md#3-model"],
      "depends_on": [],
      "acceptance": [
        {"pytest": ["tests/test_model.py"], "min_passed": 5},
        {"cmd": ["python", "tools/lint_data.py"]}
      ],
      "review": true
    },
    {"id": "M1-G1", "title": "Gate G1", "agent": "owner-gate", "description": "Owner steps.",
     "depends_on": ["M1-001"], "final": false}
  ]
}
```

- `agent`: a role with `"dispatch": true`, or `owner-gate`. Gates are never dispatched: when their dependencies are done they wait for the owner while the loop runs everything that does not depend on them; the loop stops (STOPPED) only when nothing else can run. A gate with `"final": true` cannot be passed while provisional decisions are open.
- `reviewer`: a role with `"reviewer": true` (default `evaluator`); `review: false` skips the review.
- `acceptance`: `pytest` checks pass only with exit code 0 and at least `min_passed` passing tests (skipped tests do not count; `allow_empty` accepts "no tests collected"; `interpreter` picks another key of `interpreters`); `cmd` checks pass on exit code 0. In `cmd`, every argument equal to a key of `interpreters` (`python`, `node`...) is replaced by the configured interpreter, in the agent's prompt too.
- The global checks of `project.json` run after every task; acceptance checks select the task's own tests. All checks of a task share the time budget `check_budget_min` (default 40), each at most `check_timeout_min` (default 30); a timeout kills the check's whole process tree.
- Long checks: a Bash call lasts at most 10 minutes, so `step_verify` never runs the checks itself. It starts them in a detached process (`harness/checks.py`, record `harness/logs/checks/run.json`, phase `checking`) and waits at most `check_wait_min` (default 8, at most 8.5) per call; while they run it prints `CHECKS RUNNING` and names itself as the NEXT ACTION. Fast checks finish within the first call, as before.
- Interruptions: the runner survives the end of the Bash call and of the Claude Code session, and keeps a heartbeat. Whoever runs `step_verify` next (directly, or via `step_next`, which routes the `checking` phase there) takes a finished result if HEAD did not move; otherwise, and when the runner is gone (session, machine or process stopped) or crashed, the checks start again from scratch, not counted as an attempt. After `check_restarts` (default 2) restarts without a result the task is blocked (`harness`) and the loop halts.
- Checks longer than `check_warn_min` (default 9) are logged as slow, with one notification per run of the loop: the suite is growing.

## Claims

The agent ends with `harness/claim.json`: `task_id`, `status` (`review` or `blocked`), `summary`, `notes_for_owner`; when blocked, `question`, `question_id` (the id of the entry in the open questions file) and `block_type`: `question` (an open question), `owner_action` (something only the owner can do: a gate step, a licence, a data file) or `harness` (the harness itself prevents the work). Only under `AGENT_STOP`: `status` `paused` with `done` and `todo` (the handoff note).

## Resolver (project.json `resolver.mode`: off, advise, auto)

A `question` block (with a clean working tree) goes to the `resolver` and then, always, to the `challenger`, unless the mode is `off` or `docs/owner_principles.md` is missing or still has placeholders (then the question goes to the owner as the agent wrote it, and the loop start warns).

- The **resolver** classifies the question (a `Category:` R or D id of the principles file; unknown means the owner's), says whether it needs the owner (ANSWER or ESCALATE), and writes its best answer in both cases.
- The **challenger** attacks it: AGREE, AMEND (same decision, mandatory refinements), COUNTER (a different decision) or ESCALATE. On an escalated question it challenges the recommendation the owner will read.

Who decides:

- **advise**: the owner decides every question, with the challenged recommendation. `python harness/owner_tools.py decide qid=<Q> verdict=agree|amend|differ [note="..."] [obvious=yes]` resumes the task with the decision.
- **auto**: as advise, except that a question is decided **provisionally** (the task resumes at once) when the resolver answered, the challenger agreed or amended, and the question's category is autonomous in the trust ledger. Provisional decisions are listed by every terminal output, STATUS.md and ntfy until the owner runs `ratify` or `override` (which sends the task and every task built on it since back to the queue); a `final` gate refuses while any is open The builder records a provisional decision in the open-questions entry (`## <qid>:` heading) with two fixed lines, `Status: provisional (resolver, <date>)` and `**Decision (owner):** pending ratification.`; `ratify` rewrites them in that entry to `Status: decided (ratified by the owner, ...)` and `**Decision (owner):** ratified, ...`, leaves the commit to the owner, and prints a REMINDER instead of editing when it does not find them (another layout). Applied from the queue while a task runs, the edit is recorded as an owner write. A `final` gate also refuses while an entry of a ratified or overridden decision still says provisional or pending ratification.

Trust ledger (in state.json, kept by `archive`): a delegated category (D) starts in **warm-up** and becomes **autonomous** after `warmup` answers in a row that the owner accepted (`agree` or `amend`); a `differ` restarts the count; an `override` of a provisional decision sends the category back to warm-up. Reserved categories (R) stay the owner's; when the owner marks three of their escalations `obvious=yes`, STATUS.md suggests delegating them in the principles file. `owner_tools.py trust category=<id> level=owner|warmup|auto` sets a category by hand. Caps: `max_per_run`, `max_per_task`.

## Owner controls

- Three stop files at the repository root, from the most patient; each stays until the owner deletes it, and the loop resumes where it stopped.
  - `AGENT_SOFT_STOP`: the agent at work (task agent, reviewer, resolver or challenger) finishes; the loop stops before spawning the next one. On restart it spawns that one first.
  - `AGENT_STOP`: the loop does not wait for the task. At its next tool call the task agent is told by the guard to commit its work in progress, write a `paused` claim with a handoff note and end; any other call is refused. A reviewer, resolver or challenger is told to end without writing. `step_verify` records the pause: not an attempt, no review; the next dispatch is a fresh prompt with the handoff note and the task's `git log`. A pause is recorded only when the owner asked for it (the file, or a guard refusal it caused); a `paused` claim otherwise is an invalid claim.
  - `AGENT_EMERGENCY_STOP`: every tool call of every session is blocked at once (force kill: nothing is committed, no handoff). For an agent doing damage.
- `harness/STEER.md`: a note added to the next dispatched prompt, then archived. `harness/STEER_<task id>.md`: injected into that task's next dispatch.
- Live view: the laop viewer (a separate tool, `viewer/` in the laop repository), run once for all repositories: `python viewer.py`; it reads the harness files of each repository and never writes them.
- `python harness/owner_tools.py` (no arguments for help): `status`, `pass_gate`, `unblock`, `resume`, `reverify`, `ratify`, `override`, `owner_commit`, `foreign_commit`, `allow_path`, `clear_halt`, `release`, `lint`, `archive`, `approve`, `approvals`, `owner_write` (the last three act at once: they are not loop state). While a task is in flight, actions are queued and applied by the next step script.
- `reverify task=<id>`: the fast path after the owner fixed what blocked a task whose work is already committed (a global check failing on a file outside the task's allowlist, a missing data file). The next step dispatches no agent: it takes the task's last recorded claim, adds "reverified by owner after <owner commits>", and runs `step_verify` as after an agent's claim (integrity, protected paths, acceptance and global checks), then the reviewer. A failure is an ordinary rejected attempt: the next dispatch is a real agent with the findings. Accepted for a task blocked with `block_type` harness or owner_action, or resumed and not yet dispatched, with commits of its own in its range and a recorded claim with a summary; refused for a question block (it goes through the resolver or `decide`). Use `resume` when the agent must change its work.
- Branches: the loop refuses to run on the `protected_branches`; the owner creates the milestone branch and merges it after the last gate.
- Working in parallel: a second session (the owner's) shares the working tree with the loop. While the loop is active, the guard records every repository file the owner writes there (Write/Edit tools, and shell writes it can recognise) in `harness/owner_writes.json`; the step scripts leave those files out of the task's checks (protected paths, uncommitted changes, quarantine), tell the reviewer to ignore them, and treat a commit made during a task that touches only such files as the owner's. Loop roles may not stage everything (`git add -A`, `git add .`, `git commit -a`), so they do not sweep the owner's edits into their commits. Files changed outside Claude Code (an editor, a terminal of your own) are not recorded: record one first with `owner_write path=<rel>` (the laop monitor does it before writing a decision, then commits that file alone), or commit them between tasks.
- Commit attribution: the commits of each dispatch are recorded as the task's own; owner commits made between tasks are excluded from every task's range automatically. Any other commit made while a task is dispatched needs `owner_commit`.
- Notifications: `ntfy_topic` and `ntfy_verbosity` in `project.json`.

## Guard health

- At every session start the SessionStart hook runs a self-test of the guard and the core digests and shows `<name> guard active (laop <version>)`. **If that message is missing, or a hook error appears instead, the protections are off.**
- The guard writes `harness/logs/guard_heartbeat.json` with the command it checked; each step script refuses to run unless the heartbeat shows that the guard intercepted the very call that started it.
- The guard fails closed: any internal error blocks the call with exit code 2.
- The guard records every call of a loop role it blocks in `harness/logs/guard_blocks.jsonl` (last 200). When a reviewer, the resolver or the challenger leaves no valid file, `step_record` and `step_resolve` name the calls the guard blocked since the review began, so a write the guard refused is told apart from a file never written. Each entry says who made the call (`session_id`, `agent_id`, `agent_type`, `transcript_path`, `cwd`, `hook_event_name`), its `role` and `role_source` (`subagent`; `agent_type_without_agent_id`, a main session started as that agent; `orchestrator_binding`), and what it tried: for Write/Edit/MultiEdit/NotebookEdit the `path`, `path_resolved` and `content` (`content`, `old_string`/`new_string`, `edits`, `new_source`), for Bash/PowerShell the `command`; each text is kept up to 4096 characters, with its full `length` and `sha256`. The SessionStart self-test (`session_check.py`) runs the guard with `LAOP_GUARD_SELFTEST=1`: its probe blocks (a loop role writing `CLAUDE.md`) are not logged.
- Hooks run with `hook_python` of `project.json` (`py -3` on Windows; the `python.exe` alias of the Microsoft Store does not work).

## Known limitations

- Shell analysis is heuristic; writes have the `step_verify` backstop, reads do not. Here-document bodies (`<<'EOF'`, `<<"EOF"`, `<<-`) and PowerShell here-strings (`@'...'@`) are text: only their redirection is checked. In expanded bodies (`<<EOF`, `@"..."@`) and double-quoted strings, the substituted commands (`$(...)`, backticks) are checked as commands. A body without its closing line, an unclosed quote or substitution is blocked. Newlines separate commands; `#` starts a comment only at the start of a word.
- The orchestrator is a model: the guard restricts it to the scripts and the expected dispatch; the Stop hook stops re-prompting it after repeated stops without progress.
- Every harness component encodes an assumption about what the model cannot do alone. After each model upgrade, try removing pieces and keep what still catches problems.
