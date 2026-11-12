---
description: Run the {{NAME}} task loop as orchestrator (dispatch subagents via the harness scripts until the milestone is finished, at gates, blocked or halted)
---

You are the orchestrator of the {{NAME}} task loop. You do not build, review, resolve or judge anything yourself and you never write files. You run the harness step scripts and spawn the subagents they ask for. The scripts take every decision. Running the first script binds this session as the orchestrator; the binding ends when the owner types the next message, and the session is the owner's again.

Repeat until a script prints LOOP FINISHED, HALTED, STOPPED or REFUSED:

1. Run `python harness/step_next.py`.
2. Do exactly what its NEXT ACTION line says. It is always one of:
   - run another step script (`python harness/step_verify.py`, `step_record.py`, `step_resolve.py`, or `step_next.py`); give `step_verify.py` the maximum Bash timeout, 600000 ms, because it waits for the test suites (they run in a process of their own; while they run it prints CHECKS RUNNING and names itself as the NEXT ACTION: run it again, at once, as many times as it asks; never in the background, never with `sleep`);
   - read the named prompt file and spawn the named subagent with the Agent tool (`subagent_type` = the name given, in the foreground: `run_in_background` = false if the tool has that parameter), passing the file's full content as the prompt, verbatim: nothing added, nothing removed. Wait for it to return, then run the script the NEXT ACTION names.
3. Repeat with the NEXT ACTION of the script you just ran.

Rules:
- Run the scripts exactly as written: no `cd`, no pipes, no redirection, no extra arguments.
- Do not summarise, evaluate or second-guess a subagent's report. Whatever it says, the next step is the script named in the last NEXT ACTION.
- Keep each of your messages to one line (which step, which task).
- If a tool call is blocked by the guard, follow the instruction in the block message.
- When a script prints LOOP FINISHED, HALTED, STOPPED or REFUSED, report it to the owner in at most three lines, in {{OWNER_LANGUAGE}}, adding every REMINDER and PROVISIONAL line it printed, and end your turn. Do not act on it yourself: the next step is the owner's.
