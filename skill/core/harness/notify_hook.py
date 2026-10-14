"""Notification and StopFailure hook: forwards session events to ntfy.sh (core file).

Registered in .claude/settings.json. Sends a push notification when Claude Code
waits for a permission or for input, and when a turn ends on an API error
(usage limit, overload). Does nothing unless ntfy_topic is set in harness/project.json.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import loop_core as L  # noqa: E402


def main() -> int:
    try:
        event = json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))
    except ValueError:
        return 0
    name = event.get("hook_event_name", "event")
    detail = event.get("message") or event.get("error") or event.get("notification_type") or ""
    if not isinstance(detail, str):
        detail = json.dumps(detail)[:500]
    L.notify(f"Claude Code {name}", detail or "Open the session.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
