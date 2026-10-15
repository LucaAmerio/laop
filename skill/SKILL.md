---
name: laop
description: Install, adopt (migrate from the legacy pre-laop harness), define what to build (discover: requirements and milestones of a new idea or an existing spec; brief: what one milestone means, before planning it), upgrade, extend, check or backtest (measure the resolver on past decisions) the laop task-loop harness in a repository, the agent loop where an orchestrator session dispatches builder and reviewer subagents task by task under a guard hook, with owner gates and a resolver that answers non-decisional open questions. Use when the user asks to set up the agent loop or harness in a new project ("/laop init", "set up the loop", "install the harness"), to upgrade it ("/laop upgrade"), to add a loop role ("/laop add-role"), or to check it ("/laop doctor", "is the guard active?"). Also when the owner has an idea to build with the loop, or a milestone to start ("/laop discover", "/laop brief m2", "what should m2 do?").
---

# laop: the task-loop harness

The skill directory holds the harness and its installer:

- `core/`: core files, identical in every project, replaced by upgrades (never edited in a project);
- `render/`: templates of the rendered files (loop command, hooks block of `.claude/settings.json`);
- `templates/`: config templates (CLAUDE.md, agents, owner principles) and `answers.example.json`;
- `tools/laop.py`: the installer (`init`, `upgrade`, `doctor`, `render`), standard-library Python;
- `tests/`: the harness tests (`python -m unittest discover -s <skill>/tests`).

Read `core/harness/README.md` once before any mode: it is the manual of what you install.
`<skill>` below is this skill's directory; `<repo>` the target repository. Run Python as `python`
(on Windows, `py -3` if `python` is not on PATH).

## Mode: init

1. **Preconditions.** `<repo>` is a git repository (offer `git init`), it has no
   `harness/core.lock.json` (otherwise use upgrade), and its working tree is clean. Run the skill
   tests once: `python -m unittest discover -s <skill>/tests`; stop if they fail.
2. **Interview the owner.** Ask in the owner's language, a few questions at a time, and propose a
   default for each from what you can see in the repository (stack, layout, tests). Collect:
   - name, purpose (two sentences), the owner's profile (what they are expert in), the language for
     loop reports;
   - **north star**: the one sentence every decision is judged against;
   - **principles**, asking explicitly the questions that shaped earlier projects:
     - What is the benchmark of capability (existing products, standards)? May the project ever be
       *less* capable, or only *not yet* capable (an explicit "not yet supported" error with the
       extension planned)?
     - Is backward compatibility of files, APIs and contracts needed now, or is the project
       early-stage and free to change?
     - When an earlier decision turns out wrong: correct it or keep it?
     - Which topics does the owner know first-hand, and which do they delegate (approving without
       being able to check in depth)? The resolver may answer the delegated ones; the challenger must
       then provide the scrutiny the owner cannot.
   - stack and interpreters (placeholder name → candidate paths, e.g. `python` → `.venv/...`,
     `node` → a portable install), platform (`windows` or `posix`, for the hook interpreter);
   - global checks run after every task (fast suite, linter, type check), each well under 10 minutes;
   - what the builder writes (globs) and must not write; ground-truth data (references, fixtures,
     locked directories with a marker); paths outside the repository that must stay read-only;
   - roles beyond planner, builder and evaluator: a second reviewer (UI), a blind role that must not
     see some files (independent checks), runners of external programs (with a reminder line);
   - git-ignored paths a task must not change (`integrity_paths`, e.g. the venv's site-packages);
   - ntfy topic for phone notifications (optional); the resolver mode (`resolver_mode`): `advise`
     (default: every question reaches the owner with a recommendation of resolver and challenger),
     `auto` (as advise, and delegated categories become autonomous after a warm-up), or `off`.
     Explain that the resolver waits until `docs/owner_principles.md` has no placeholders, and that
     the categories (R reserved, D delegated) of that file decide what it may ever decide alone:
     topics the owner delegates because they cannot check them in depth are exactly the D ones.
   - Do not put a whole virtual environment in `integrity_paths` unless it is needed: its digest
     covers the installed distributions only, but it still walks the directory at every task.
3. **Write the answers** to a JSON file in your scratchpad, in the format of
   `<skill>/templates/answers.example.json` (extra roles go in `extra_roles`, as roles.json entries).
4. **Install:** `python <skill>/tools/laop.py init <repo> <answers.json>`. It copies the core,
   writes the config files (an existing file is kept and a `.laop` proposal written next to it),
   renders the loop command and the hooks, appends the runtime block to `.gitignore`, and writes
   `harness/core.lock.json`. It lists the placeholders left to fill.
5. **Customise the config files** (they are the owner's from now on; core files are not):
   - fill every placeholder listed, and the `Code conventions` section of `CLAUDE.md` with the
     project's real conventions;
   - adapt `.claude/agents/builder.md` and `evaluator.md` to the stack (how to run the tests, what
     to check independently, domain pitfalls); for extra roles, start from
     `<skill>/templates/agents/optional/` and write `.claude/agents/<role>.md`;
   - complete `docs/owner_principles.md` from the interview: section 0 in the owner's words,
     project-specific reserved and delegated categories (R and D rows), escalation signals;
   - merge any `.laop` proposal with the existing file, with the owner.
6. **Check:** `python <skill>/tools/laop.py doctor <repo>`; fix every PROBLEM. Show the owner the
   diff, and commit only with their approval (`laop: install harness <version>`).
7. **Tell the owner the next steps:** restart the Claude Code session (hooks load at start) and
   look for "<name> guard active"; run `/laop discover` (spec and roadmap), then `/laop brief m1`;
   create a milestone branch (`git switch -c m1`); have the `planner` subagent write
   `harness/tasks.json`; review it; type `/laop-start`. The loop refuses to start until spec,
   roadmap and brief are approved (`require_brief`, default on). To follow the
   loop live: the laop viewer (`python <laop>/viewer/viewer.py`, one viewer for every
   repository; add this one with its "+" tab).

## Mode: discover (what the project must do, and in which milestones)

Once per project, before the first plan, and again when the scope changes. The owner may start
from an idea only, or from a specification written elsewhere (import). Output: `docs/spec.md` and
`docs/plan/roadmap.md`, approved by the owner. The `analyst` and `spec-reviewer` roles must exist
(`init` writes them; for an older install see `CHANGELOG.md` 0.4.0).

Subagents cannot talk to the owner: **you run the interview**, the `analyst` subagent writes.

1. **Seed.** Ask the owner to describe the idea freely, in their language. Import: ask for the
   source document(s), copy them unchanged to `docs/source/` and read them.
2. **Coverage.** Mark each interview topic Clear, Partial or Missing (from the seed or the source):
   users and goals; core behaviours; inputs and outputs; data and its ground truth; quality
   attributes with numbers; integrations and external programs; platforms and constraints; errors
   and edge cases; non-goals; risks; how the owner will judge the result. Show the table.
3. **Question rounds.** Rank the Partial and Missing topics by impact on what gets built times
   uncertainty; ask at most 6 questions per round, batched (the owner's time is the bottleneck),
   each with your recommended answer and one sentence on why it matters. Multiple choice where
   possible. Interviewer rules: one question per item (no compound questions), no
   leading questions, no jargon the owner did not use, never assume an answer they did not give.
   Import: ask only about gaps, ambiguities and contradictions of the source. Stop when no topic is
   Missing and no open answer would change a milestone; anything left becomes a marker.
4. **Milestones.** Propose the cut: P1 stories first, the first milestone ends with something the
   owner can use; each milestone has exit criteria. Scope is the owner's (reserved): ask them to
   confirm or change the order and content.
5. **Draft.** Spawn the `analyst` subagent (foreground) in mode discover, with the interview notes
   (questions and answers verbatim, coverage table, milestone cut, source paths).
6. **Review.** Spawn the `spec-reviewer` subagent on the two documents. Fix CRITICAL and HIGH
   findings with the owner (ask again, then re-run the analyst on the affected sections); every
   `[NEEDS CLARIFICATION]` marker is answered or the owner moves it out of scope.
7. **Approval.** Show the owner both documents and the review counts. The owner approves with
   `python harness/owner_tools.py approve doc=spec` and `approve doc=roadmap`; commit
   `harness/approvals.json` with their approval. Never run `approve` yourself unless the owner tells
   you to in this conversation.

## Mode: brief (before each milestone: what does it mean?)

Mandatory before the planner runs (`require_brief`); its cost scales with the uncertainty.

1. **Draft first.** Spawn the `analyst` in mode brief for milestone `<m>`, with any notes the owner
   gave. It fills the brief from the roadmap, the code, ADRs, decided questions and earlier
   milestones, and returns the coverage table and its markers.
2. **Light path**: no Partial/Missing topic, no marker, nothing in "Changes since the roadmap" that
   contradicts the roadmap. Show the owner the brief and a short list "what I assumed"; ask for
   confirmation or corrections.
   **Full path** otherwise: question rounds as in discover (step 3), on the gaps and changes only;
   then re-run the analyst with the answers.
3. Spawn the `spec-reviewer` on the brief (with roadmap and spec as context). Fix CRITICAL and HIGH.
4. The owner approves: `python harness/owner_tools.py approve brief=<m>`. If the brief changed the
   scope of the milestone, the roadmap is updated by the analyst and approved again too.
5. Next: milestone branch, planner, review of `tasks.json`, loop. The brief's "Owner decisions"
   become the first gate.

## Mode: adopt (a repository that runs the legacy pre-laop harness)

For repositories with `harness/loop_core.py` and `.claude/hooks/guard.py` but no
`harness/core.lock.json`.

1. **When.** Only between milestones: the previous milestone finished and merged, no task in flight,
   no loop active (`adopt` refuses otherwise). Ask the owner to confirm, and to stop any loop session
   of that repository.
2. **Preconditions.** Clean working tree; a dedicated branch (`git switch -c laop-adopt`). Run the
   skill tests once.
3. **Run:** `python <skill>/tools/laop.py adopt <repo> [principles=<file>]`. It reads the previous
   configuration by importing the old `loop_config`, `pathrules` and `loop_core`, then:
   - writes `harness/project.json` (name from the old command file; interpreters,
     global checks, limits, ntfy from `loop_config`; locked case directories, the protected sibling
     repository and the external programs from the old code);
   - converts `harness/roles.json` (core roles removed; `dispatch` for the old `VALID_AGENTS`,
     `reviewer` for the old reviewers) and the old `"python": "oracle"` checks of `tasks.json`;
   - replaces the core files, renders the loop command `/laop-start` (the old `/<name>-loop` file
     is removed) and the hooks block, keeps the
     project's agents, CLAUDE.md and permissions;
   - keeps `state.json` (binding cleared; a backup goes to `harness/logs/`).
4. **Act on every ACTION REQUIRED line**, with the owner (and offer `/laop discover` in import mode
   for the existing specification, plus `/laop brief` before the next milestone):
   - remove the tests of the previous harness it lists (they would fail every global check);
   - update CLAUDE.md: the claim's `question_id` and `block_type`, `owner_tools.py` with arguments,
     and the resolver if it will be enabled;
   - write `docs/owner_principles.md`, or pass an existing file with `principles=`;
   - check the project's own agents for references to the old harness (`harness/MAIN_ROLE`, the
     `ACTION =` block of owner_tools).
5. Run doctor, show the diff, commit with the owner's approval (`laop: adopt <version>`), restart the
   session, check "guard active", then run one small task before a whole milestone.
6. Rollback: every harness file is tracked, so `git revert` of the adoption commit, plus the
   state backup copied back to `harness/state.json`.

## Mode: backtest (measure the resolver on questions the owner already decided)

1. **Prepare:** `python <skill>/tools/backtest.py prepare <repo> <out> [questions=loop|all|Q34,Q61]
   [principles=<file>] [classification=<file>]`, with `<out>` in your scratchpad. For each question:
   a copy of the repository at the commit that raised it (no git history), the owner's decision
   removed, the principles without the rows derived from that question or later ones, and three
   prompts. Read `<out>/manifest.json`.
2. **Tell the owner the cost before running:** three subagents per question (resolver and challenger
   read the repository: roughly 100k tokens each; the judge is small). Propose a pilot of about six
   questions mixing the owner's agreements and disagreements, then the rest.
3. **Run**, for each question of the manifest, in this order and each in the foreground, a
   `general-purpose` subagent with the full content of `resolve_prompt.md`, then one with
   `challenge_prompt.md`, then one with `judge_prompt.md`. Different questions are independent:
   several may run in parallel. Never show the judge's prompt (it holds the owner's decision) to
   the resolver or the challenger.
4. **Report:** `python <skill>/tools/backtest.py report <out>`. Explain to the owner: decisions the
   resolver would have taken alone and how many were wrong (the number that matters, it should be
   zero), the quality of the recommendations, escalation of the questions where the owner had
   information of their own, and the trust ledger the history gives. Categories that end
   autonomous can be set with `python harness/owner_tools.py trust category=<id> level=auto` once
   the project runs laop.

## Mode: upgrade

1. `python <skill>/tools/laop.py doctor <repo>` to see the installed version and any local change.
2. Read `<skill>/CHANGELOG.md` for the versions between the installed one and this one; tell the
   owner what changes and whether any config file needs a manual edit.
3. A loop in progress: do not upgrade while a task is in flight (`python harness/owner_tools.py
   status` shows it); wait for a stop.
4. `python <skill>/tools/laop.py upgrade <repo>`. If it refuses because core files were modified
   locally, show the owner the diff of each file (`git diff` or against `<skill>/core/`). A local
   fix worth keeping belongs in the skill's core (then upgrade); otherwise `--force` with the
   owner's approval.
5. Run doctor again, show the diff, commit with the owner's approval
   (`laop: upgrade to <version>`). The session must be restarted for new hooks to load.

## Mode: add-role

1. Ask: the role's purpose; dispatched for tasks, or a reviewer; what it writes; what it must not
   see (`deny_read`, for independence); shell or git restrictions (`bash_allow`, `git_allow`);
   whether it needs an external program (then add an `external_programs` entry with a reminder).
2. Add the role to `harness/roles.json` (never a core role name: owner, orchestrator, resolver,
   challenger). A blind role's `deny_read` lists files, and the guard also refuses recursive searches
   of directories that contain them: keep hidden files in directories the role does not need to search.
3. Write `.claude/agents/<role>.md` (start from `<skill>/templates/agents/optional/` when one fits);
   its `name` must equal the role name.
4. Tell the planner's file which tasks the role takes, if the role is dispatched.
5. Run doctor; the session start self-test tests the first dispatched role only, so also probe the
   new role by hand if it is blind: ask a subagent of that role to read a hidden file and check that
   the guard refuses it.

## Mode: doctor

Run `python <skill>/tools/laop.py doctor <repo>` and explain each line to the owner: installed and
skill versions, roles, the guard self-test, local changes to core files, missing agent files, the
`.gitignore` block. Suggest the fix for each PROBLEM; apply it only with the owner's approval.

## Rules for this skill

- Never edit a core file inside a project, and never install over an existing harness: upgrades
  go through `upgrade`.
- Never commit, push or switch branches without the owner's approval.
- The owner's principles are per project: never carry one project's principles into another.
