# laop changelog

## 0.4.3 (2026-10-18)

- New owner action `reverify task=<id>`: a task whose work is already committed goes straight to the
  harness checks and the reviewer, without a new task agent. Before, `resume` and `unblock` always
  dispatched an agent, which re-read the context, re-ran the fast suite and wrote a claim before
  step_verify ran the same suite again (Talos M6A-026 and M6A-028, blocked only by a global check
  failing on a file outside the case-writer's allowlist that the owner fixed: about 20 min per task
  of agent session that added nothing).
  - Accepted for a task blocked with block_type `harness` or `owner_action`, or resumed and not yet
    dispatched, that has commits of its own in its range (owner and foreign commits excluded) and a
    recorded claim with a summary. A question block is refused: it goes through the resolver or
    `decide`. A refusal changes nothing and queues nothing.
  - Like `resume`, it keeps base and attempts, and is queued while a task is in flight.
  - When the loop picks the task, `step_next` spawns nothing (`REVERIFY: ...`): it writes the last
    claim again (status review, same summary and notes_for_owner, plus "reverified by owner after
    <commits since the task's last own commit>") and `step_verify` runs as after an agent's claim:
    integrity, protected paths, acceptance and global checks, then the reviewer and `step_record`.
    A failure is an ordinary rejected attempt (counted, findings recorded); the next dispatch is a
    real agent with the findings.
  - `step_verify` keeps each task's last valid claim in state.json (`last_claim`), since
    `harness/claim.json` is deleted at the next dispatch; for a task blocked before 0.4.3,
    `harness/claim.json` is used when it is still that task's.
  - Logged as "owner reverify <id>"; STATUS.md shows the task as `todo (reverify)` and lists it under
    "Owner reverify". The block message of a harness or owner_action block (STATUS.md, ntfy) says
    that after the fix `reverify` is the fast path and `resume` the path when the agent must change
    its work. Help text of owner_tools.py and harness/README.md updated.
- The guard no longer records a here-document delimiter as a file the owner wrote. When the closing
  line was not exact (a trailing blank, an indented `EOF`), the owner's command was unparsable and
  the guard read the raw text as one command: in `cp a tests/x.json` + newline + `git commit -q -F -
  <<'EOF'` ... `EOF`, the delimiter and words of the message became `cp` operands and landed in
  `owner_writes.json` (Talos: "owner edits ... ignored by the checks: ['EOF', ...]"). For the owner,
  a here-document without an exact closing line now runs to the end of the command, as bash reads
  it, and a command still unparsable is read a line at a time. Loop roles still fail closed.

## 0.4.2 (2026-10-17)

- The guard reads here-document bodies as text. Before, it parsed them as shell: a verdict written
  with `cat > harness/verdicts/<task>.md <<'EOF'` whose text contained `Display > Local axes` was
  blocked as a write to `Local`, outside the reviewer's allowlist (Argus M1-021 halted twice). Now
  (`strip_heredocs`), before the analysis:
  - the bodies of `<<'EOF'`, `<<"EOF"`, `<<\EOF` and `<<-` are removed: no redirection, no command;
  - in the bodies of `<<EOF` (expanded) the substituted commands, `$(...)` and backticks, are
    still checked as commands, also inside `$((...))` and `${...}`;
  - the same for PowerShell here-strings: `@'...'@` is text, in `@"..."@` the `$(...)` are checked;
  - the redirection of the here-document (`cat > file <<'EOF'`) is checked as before;
  - fail closed: a here-document or here-string without its closing line, a quote or a
    substitution not closed is blocked ("... has no closing line").
  Here-strings `<<<` and `>` inside quotes were already read correctly; tests now cover them.
- Three ways a loop role could get a write past the guard, found while fixing it, are closed:
  a newline did not separate commands (`echo hi` + newline + `rm -rf src` was read as one `echo`);
  a `#` inside a word started a comment (`echo x#; rm -rf src`: the `rm` was dropped); a `$(...)`
  inside double quotes was not checked (`echo "$(rm -rf src)"`). The guard now drops comments only
  at the start of a word, splits commands at newlines and checks every substituted command.
- The guard records each blocked call of a loop role in `harness/logs/guard_blocks.jsonl` (last
  200). When the reviewer leaves no valid verdict, `step_record` says how many of its calls the
  guard blocked since the review began and the last one, in the retry message and in the HALT; so
  does `step_resolve` for the resolver and the challenger. Before, telling "did not write the file"
  from "the guard refused the write" required reading the subagents' transcripts.
- Reviewer prompts: the verdict (resolution) file is written with the Write tool, never through
  Bash; the reviewer waits for the result of the Write before its final reply, never in the same
  message, rewrites it if it failed, and never replies with a verdict that is not in the file.
  Core files: `resolver.md`; `challenger.md` appends its section with the Edit tool (its tools are
  now Read, Glob, Grep, Bash, Edit). Templates (new projects; existing ones keep their agent
  files): `evaluator.md`, `optional/ui-reviewer.md`.

## 0.4.1 (2026-10-16)

- `ratify` updates the tracked open-questions file, the only durable record of the decision. Before,
  it changed only state.json (git-ignored): the entry kept saying "provisional" and "pending
  ratification", STATUS.md showed nothing pending, and the final gate passed the mismatch along.
  Now, in the entry of the qid (from its `## <qid>:` heading to the next `## `), every
  `Status: provisional (resolver, <date>)` (also wrapped) becomes `Status: decided (ratified by the
  owner, <today>; provisional decision of the resolver, <date>)` and `**Decision (owner):** pending
  ratification.` becomes `**Decision (owner):** ratified, <today>: ...`. Line endings are kept, the
  owner commits. When the entry or the status line is not found (another layout), nothing is edited
  and a `REMINDER` names the file and the qid. A ratify queued while a task is in flight is applied
  by the step script the same way, and the file is recorded in `owner_writes.json`, so it does not
  count against the task.
- The dispatch prompt of a provisional decision gives the builder the two exact lines to write; a
  decision the owner ratified before the dispatch is recorded as decided at once.
- `override`'s steer note asks the reopened task for `Status: decided (owner, <date>)` in place of
  the two provisional lines.
- `pass_gate` on a `final` gate refuses while the entry of a ratified or overridden decision still
  says provisional or pending ratification.
- New projects: the open-questions file's header names the `## Qn: <title>` heading.

- `doctor` reports `UPGRADE AVAILABLE` when the installed core files differ from the skill's, also
  at the same version (unreleased changes). `/laop brief` runs doctor first and upgrades before the
  milestone when it does.

- Findings are kept per task in `harness/findings/<task>.md`, a section per rejected attempt,
  appended and never overwritten (`harness/NEXT_FINDINGS.md`, one file for the whole loop, is
  only read for a task that has no findings file yet). Before, a rejection overwrote the findings
  of the previous one, also of another task: a missing claim erased a reviewer's NEEDS_WORK, and a
  blocked task's `blocked_reason` pointed at findings that were gone. The dispatch prompt carries
  the latest reviewer verdict and every harness finding after it; `blocked_reason` names the
  task's file; `owner_tools.py archive` moves `harness/findings` to `harness/logs` with the state.
- An empty run (no claim, nothing committed or left uncommitted) is counted from the commit at
  dispatch, not from the task's base: a session that died after an earlier rejected attempt no
  longer consumes an attempt, and the base with the earlier commits is kept. A dispatch cut short
  by an API error (a `StopFailure`, now recorded by `notify_hook.py` in
  `harness/logs/stop_failure.json`) is not an attempt either, even with commits; the next prompt
  says the session was interrupted. Both count towards `max_empty_runs`.

- The loop command is `/laop-start` in every project (it was `/<name>-loop`, set by
  `loop_command`). `loop_command` is gone from the answers and `project.json`; `upgrade` drops it
  and removes the old `.claude/commands/<name>-loop.md`.
- `harness/state.json` keeps the last 10000 events instead of 50.
- `max_iterations` counts the dispatches of the current run: the counter starts from zero every
  time the loop starts (after a finish, a stop, a halt or a gate), not once per milestone.
- `owner_tools.py` checks an action before queueing or applying it: a missing or unknown `task=`,
  a `pass_gate` on a task that is not a gate, a missing `commit=`/`path=`/`qid=`, a `qid` with no
  open provisional decision or question, a bad `verdict` or `trust` level are refused at once
  (`REFUSED: ...`, exit code 1, nothing queued). Before, `pass_gate` without `task=` printed
  "pass_gate applied." and the step script dropped it later ("owner action not applied"). An
  action the step script still refuses is reported as refused, not as applied.

## 0.4.0 (2026-10-14)

Before the plan: what to build, and what each milestone means (decided with the owner).

- New modes `/laop discover` (once per project: interview, then `docs/spec.md` and
  `docs/plan/roadmap.md`; from an idea, or importing a specification written elsewhere into
  `docs/source/`) and `/laop brief <m>` (before each milestone: `docs/plan/<m>.brief.md`, light path
  when the roadmap already settles it, full interview on the gaps otherwise). The skill runs the
  interview; the new `analyst` role writes, the new `spec-reviewer` role reviews read-only.
- Mechanisms taken from Spec Kit, Kiro and requirements-elicitation research: coverage table
  Clear/Partial/Missing over fixed interview topics, batched questions ranked by impact x
  uncertainty with a recommendation each, `[NEEDS CLARIFICATION]` markers, prioritised
  independently testable stories, EARS requirements and exit criteria, a read-only consistency
  review with severities, a pre-mortem in every brief.
- `harness/approvals.py` and `owner_tools.py approve doc=spec|roadmap|<path>` / `approve
  brief=<m>` / `approvals`: the owner's approval is the document's digest in
  `harness/approvals.json` (tracked; no role may write it); an edited document is `stale` and needs
  a new approval; a document with a marker or a placeholder cannot be approved.
- `owner_tools.py lint` (and the loop start) warn about a milestone planned without approved spec,
  roadmap and brief. With `"require_brief": true` in `project.json` `step_next.py` refuses to start
  the loop. New installs set it; upgraded projects keep it off until they opt in.
- Planner: checks the approvals first and refuses to plan without them; cannot write spec, roadmap
  or briefs (`deny_write`); puts the brief's owner decisions into the first gate and covers every
  exit criterion. `doctor` reports the approvals and the next step.

Upgrading from 0.3.x (manual, config files are the owner's):
1. `harness/roles.json`: add
   `"analyst": {"allow_write": ["docs/spec.md", "docs/plan/roadmap.md", "docs/plan/*.brief.md", "<open questions file>"], "git_allow": ["status", "log", "diff", "show", "rev-parse", "ls-files", "add", "commit"]}`,
   `"spec-reviewer": {"allow_write": ["docs/plan/reviews/**"], "git_allow": ["status", "log", "diff", "show", "rev-parse", "ls-files"]}`,
   and to `planner` `"deny_write": ["docs/spec.md", "docs/plan/roadmap.md", "docs/plan/*.brief.md"]`.
2. Copy `templates/agents/analyst.md` and `spec-reviewer.md` to `.claude/agents/` (fill `{{NAME}}`
   and `{{OPEN_QUESTIONS}}`), and merge the "Before planning" section of `templates/agents/planner.md`
   into the project's planner.
3. Run `/laop discover` (import mode for an existing specification) and `/laop brief` for the next
   milestone; then set `"require_brief": true` in `harness/project.json`.

## 0.3.2 (2026-10-12)

- The viewer leaves the harness: it is now laop_view, a separate tool (`viewer/` in the laop repository)
  that one runs once for every repository. Upgrading removes `harness/viewer.py` and
  `harness/viewer.html` from the repositories; the guard rule on `viewer.py` goes with them.
- Backtest judge: a `DEFECT` line marks the questions where the recommendation differs from the
  owner's decision because it shows a concrete case that decision gets wrong; the report lists them
  as decisions to check in the project, not as errors of the recommendation. (Found by the first
  backtest pilot: 4 of the 6 differences were of that kind.)

## 0.3.1 (2026-10-12)

- Viewer, several repositories: one tab per repository and an Overview tab with a card for each
  (status, progress bar, the task in flight and its phase, gates awaiting the owner, questions to
  decide, provisional decisions, blocked tasks with their reason, last activity). Each tab shows
  how many items wait for the owner. The "+" tab asks for the path of a repository (and lists the
  ones with a task loop found nearby); x stops watching one. The list lives in
  `~/.laop/viewer.json` (`config=` for another), so any viewer, started from any repository, shows
  them all.
- Viewer: gates are drawn as gold hexagons labelled OWNER GATE (dashed while not reached, green
  once passed), marked in the task table and in the chips.
- Viewer: questions waiting for `decide` (0.3.0) show the recommendation and the three decide
  commands instead of `unblock`.
- Viewer server: answers only its own page (Host and Origin checks; changes accept JSON only), so
  a web site open in the same browser cannot read the loops or change the list; snapshots are
  cached per repository until a watched file changes.

## 0.3.0 (2026-10-12)

Resolver with growing trust (decided with the owner).

- `resolver.mode`: `off`, `advise` (default for new projects: every question reaches
  the owner with a recommendation prepared by the resolver and attacked by the challenger), `auto`
  (as advise, and questions of autonomous categories are decided provisionally). The old
  `"enabled": true` reads as `auto`.
- The challenger now always runs, also on escalated questions, so that the owner always decides
  on a challenged recommendation. The resolver writes a `Category:` line (an R or D id of the
  principles file), a plain-words summary and the alternatives.
- `owner_tools.py decide qid=<Q> verdict=agree|amend|differ [note=...] [obvious=yes]`: one command
  to answer a question; the task resumes with the recommendation (agree), with the owner's note
  (amend) or with the owner's decision (differ). Questions waiting for it are listed in STATUS.md
  and in every terminal output.
- Trust ledger per category (state.json, carried over by `archive`): delegated categories start
  in warm-up and become autonomous after `warmup` (3) accepted answers in a row; a `differ` restarts
  the count; an override of a provisional decision demotes the category. Reserved categories stay
  the owner's; three `obvious=yes` on one of them suggest delegating it. `owner_tools.py trust`
  sets a category by hand.
- The resolver stays inactive while `docs/owner_principles.md` is missing, has placeholders or
  defines no category; the loop start and `doctor` say so.

- `tools/backtest.py` and the `/laop backtest` mode: replay questions the owner already decided
  (copy of the repository at the commit that raised each one, decision hidden, principles without
  the rows derived from it or later), run resolver, challenger and a judge, and report the
  decisions auto mode would have taken alone, how many differ from the owner's, and the trust
  ledger the history gives.
## 0.2.3 (2026-10-12)

- Viewer: the graph spans the full width of the window (Now, Needs you and Events side by side
  above it), lays out left to right or top to bottom, fits the width (the whole window in full
  screen) or zooms, and has a full-screen mode.
- Viewer: time per task, from the whole loop log (not only the last 50 events): work time (agent,
  checks, review, resolver) summed over its dispatches, first dispatch to done, and the owner's
  wait on each gate; it ticks for the task in flight. Shown on the graph, in the task table, in the
  task details and as the milestone total.
- Viewer: "Needs you" warns when the running server is older than the page (viewer.py replaced
  after it started): restart it. Found in a real project: the page is re-read at each request, the
  script only at start, so a copied viewer showed the new page without the times. The three top
  panels now start at the same height.

## 0.2.2 (2026-10-12)

Found in the first real project.

- Integrity of `site-packages`: the digest now covers the installed distributions (`*.dist-info`,
  `*.egg-info`) and the `.pth` files only, and other directories skip byte-code and tool caches.
  Before, the `.pyc` files Python writes the first time a task imports a package halted the loop.
- Owner working in parallel: the guard records the repository files the owner writes while the
  loop is active (`harness/owner_writes.json`); step_verify and step_next leave them out of the
  task's checks, the reviewer is told to ignore them, and a commit made during a task that touches
  only such files is recorded as the owner's. Before, the owner's uncommitted answers to open
  questions were taken for the task's work.
- Loop roles may not stage everything (`git add -A`, `git add .`, `git add -u`, `git commit -a`);
  CLAUDE.md template rule 6 says why.
- `upgrade` adds the runtime entries missing from `.gitignore`.

## 0.2.1 (2026-10-12)

- `harness/viewer.py` and `viewer.html`: a live view of the loop for the owner. Run
  `python harness/viewer.py` in a terminal of its own; it serves one page on 127.0.0.1 that polls
  every two seconds: progress by status, the dependency graph (gates, blocked and ready tasks, what
  each task holds up), the task in flight with its phase (agent, checks, review, resolver,
  challenger) and elapsed time, a "Needs you" panel (halt, gates, provisional decisions, blocked
  tasks, queued actions, stale binding, no activity) with the owner_tools command to copy, events,
  loop log, commits per task, verdicts and resolutions. Read-only, no lock taken; the dispatch
  token is never shown. `snapshot=<file>` writes one self-contained HTML file. Optional desktop
  notifications.
- Guard: loop roles may not run `harness/viewer.py`.

## 0.1.4 (2026-10-12)

- Messages from other Claude sessions (`<cross-session-message ...>`), side-session and CI events
  no longer release the orchestrator binding: only a prompt the owner types does. Found in the
  sandbox: a message sent from another session released it.
- Verified live in the sandbox (0.1.3): subagent reports no longer release the binding; a
  question answered by the resolver, amended by the challenger (`AMEND`), recorded as provisional
  by the builder, reviewed, and ratified with `owner_tools.py ratify`.

## 0.1.3 (2026-10-12)

Found by the first complete sandbox loop.

- Security: a subagent's report reaches the orchestrator as a user-role message
  (`<agent-message ...>`), and the UserPromptSubmit hook took it for an owner prompt. Every
  subagent return released the orchestrator binding, so the following step scripts and the
  review, resolver and challenger spawns ran as owner, without the dispatch check, until the next
  step_next. The hook now ignores subagent reports, task notifications and Stop hook feedback,
  and logs the start of each prompt that releases the binding.
- Challenger verdict `AMEND`: the same decision with mandatory implementation refinements within
  the delegated scope; the task resumes with decision and amendments. `COUNTER` is kept for a
  different decision and still escalates. Before, a challenger that agreed on the policy but fixed
  an implementation defect stopped the loop.

## 0.1.2 (2026-10-12)

Found by the sandbox loop.

- Integrity check: an owner action queued during a dispatch and applied by a step other than
  step_verify (e.g. step_next printing PENDING) moved the state away from the dispatch baseline
  and halted the loop. The baseline now moves with owner actions when the state was intact
  before them; tampering before an owner action is still caught.

## 0.1.1 (2026-10-12)

Found by the first real loop (sandbox).

- Guard: a dispatch without `run_in_background` is accepted (the parameter is absent when
  `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS` is set, as in the default settings); only an explicit
  `true` is refused. 0.1.0 refused every dispatch with the default settings.
- Clearer PENDING message: verifying a dispatch whose subagent never ran is an empty run, not a
  failed attempt.
- Default settings allow the project interpreters and read-only shell commands.

## 0.1.0 (2026-10-12)

First version.

- Core, rendered and config files separated; project settings in `harness/project.json`
  (interpreters, global checks, limits, lockable and protected paths, external programs,
  integrity paths, resolver, ntfy) instead of code; `harness/core.lock.json` and a drift check at
  every session start.
- Core roles (owner, orchestrator, resolver, challenger) defined by the harness; project roles in
  `roles.json` with `dispatch` and `reviewer` flags.
- Guard: `Grep` and `Glob` read-checked; recursive searches of directories containing hidden files
  refused; loop roles may not run the step scripts or state-changing owner tools; the heartbeat
  records the command; foreground dispatch enforced; generic dispatch through `state.current.spawn`.
- Step scripts: a total time budget for the checks of a task; integrity of git-ignored files
  (state, verdicts, resolutions, `integrity_paths`) checked after every dispatch; a step lock and a
  queue for owner actions, so that a long step never overwrites them; claim `block_type`
  (`question`, `owner_action`, `harness`); task lint (paths outside the agent's allowlist,
  acceptance checks repeating a global check).
- Resolver phase (off by default): resolver and challenger answer or escalate open questions using
  `docs/owner_principles.md`; agreed answers are provisional decisions, ratified or overridden by
  the owner; a `final` gate cannot be passed while any is open.
- `owner_tools.py` takes command-line arguments (it is a core file and is not edited).
