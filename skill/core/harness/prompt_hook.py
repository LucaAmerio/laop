"""UserPromptSubmit hook: a prompt typed by the owner releases the orchestrator binding (core).

The guard binds a session as the loop orchestrator when it runs ``harness/step_next.py``
(``orchestrator_session`` in harness/state.json). That session keeps the orchestrator
restrictions after the loop ends: it gets the owner's rights back only when the owner types a
prompt in it, never by itself. Typing the loop command releases and then binds again.

Not every user-role message is the owner's: a subagent's report is delivered to the
orchestrator as a message (``<agent-message ...>``), and so are messages from other Claude
sessions (``<cross-session-message ...>``), the Stop hook's feedback and task notifications. Those never release the binding (laop 0.1.3: before, every subagent report
made the orchestrator the owner until the next step_next).

This hook never blocks a prompt: every problem is ignored (exit 0). An unreadable state.json is
reported by the guard, which fails closed.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import loop_core as L  # noqa: E402
import statefile  # noqa: E402

#: Messages delivered as user turns that the owner did not type.
NOT_TYPED = re.compile(
    r"^\s*(<agent-message\b|<cross-session-message\b|<task-notification\b|<system-reminder\b|"
    r"<side-sessions-event\b|<ci-monitor-event\b|Stop hook feedback:|"
    r"\[Subagent hand-back\]|Another Claude session sent a message)", re.IGNORECASE)


def typed_by_owner(prompt: str | None) -> bool:
    """False for messages the owner did not type. Without a prompt field, assume the owner."""
    return prompt is None or not NOT_TYPED.match(prompt)


def main() -> int:
    try:
        event = json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))
    except ValueError:
        return 0
    if not isinstance(event, dict):
        return 0
    session = event.get("session_id")
    prompt = event.get("prompt") if isinstance(event.get("prompt"), str) else None
    if not session or not typed_by_owner(prompt):
        return 0
    released = []

    def change(state: dict) -> None:
        if state.get("orchestrator_session") == session:
            state["orchestrator_session"] = None
            released.append(session)

    try:
        if L.STATE_FILE.exists():
            statefile.update(L.STATE_FILE, change)
    except (OSError, ValueError, statefile.LockBusy):
        return 0
    if released:
        start = " ".join((prompt or "").split())[:60]
        L.log(f"orchestrator binding released by an owner prompt (session {session}): {start!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
