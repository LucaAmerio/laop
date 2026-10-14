"""Stop hook: keeps the orchestrator working while the loop is active (core file: do not edit).

Registered on the Stop event. When the main conversation is the orchestrator and the loop is
active, ending the turn is blocked (exit code 2) and Claude is told how to continue. The stop is
allowed when the loop is not active, when AGENT_STOP exists, or after
``max_stops_without_progress`` consecutive stops with no change in the loop state (so a stuck
orchestrator cannot loop forever).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import loop_config as C  # noqa: E402
import loop_core as L  # noqa: E402
import pathrules  # noqa: E402
import statefile  # noqa: E402

MESSAGE = (
    f"The {C.NAME} loop is still active. Do not end your turn. Continue the /{C.LOOP_COMMAND} "
    "procedure: run python harness/step_next.py and follow the NEXT ACTION it prints. Stop only "
    "after a harness script prints LOOP FINISHED, HALTED, STOPPED or REFUSED."
)


def main() -> int:
    try:
        event = json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))
        main_role = pathrules.main_role(L.REPO, event.get("session_id"))
    except ValueError:  # bad input or unreadable state.json: do not keep the session going
        return 0
    if event.get("agent_id") or main_role != L.ORCHESTRATOR or L.STOP_FILE.exists():
        return 0
    outcome = {"kind": "inactive"}

    def change(state: dict) -> None:
        if not state.get("loop_active"):
            return
        fp = L.fingerprint({"tasks": {}, **state})
        guard = state.get("stop_guard") or {}
        stops = guard.get("stops", 0) + 1 if guard.get("fingerprint") == fp else 0
        state["stop_guard"] = {"fingerprint": fp, "stops": stops}
        outcome["kind"] = "stuck" if stops >= C.MAX_STOPS_WITHOUT_PROGRESS else "continue"

    try:
        statefile.update(L.STATE_FILE, change)
    except (OSError, ValueError, statefile.LockBusy):
        return 0
    if outcome["kind"] == "inactive":
        return 0
    if outcome["kind"] == "stuck":
        L.notify("orchestrator stopped", "The orchestrator ended its turn repeatedly without "
                 f"progress. Open the session and type /{C.LOOP_COMMAND} to resume.")
        return 0
    print(MESSAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
