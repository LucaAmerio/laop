"""SessionStart hook: self-test of the guard and check of the core files (core file: do not edit).

Runs .claude/hooks/guard.py on fake events built from the project's own configuration:

- a loop role writing CLAUDE.md must be blocked;
- the same role writing a file inside its allowlist must be allowed;
- the owner writing inside each protected path must be blocked.

Then compares the core and rendered files with harness/core.lock.json: a modified core file is
reported (core files are replaced by '/laop upgrade'; local changes would be lost or block it).

On success it shows '<name> guard active'; on failure an error notice. If this hook cannot start
at all (Python not found), Claude Code shows a hook error instead: in every case, the absence of
the 'guard active' message means the protections are off.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "harness"))

LOCK_FILE = REPO / "harness" / "core.lock.json"


def run_guard(event: dict) -> tuple[int, str]:
    """Run the guard on one fake event; return (exit code, stderr)."""
    guard = REPO / ".claude" / "hooks" / "guard.py"
    event = {"hook_event_name": "PreToolUse", "cwd": str(REPO), "session_id": "session-selftest", **event}
    # LAOP_GUARD_SELFTEST: the guard does not log these blocks in harness/logs/guard_blocks.jsonl
    # nor write its heartbeat: they are the self-test, not calls of an agent.
    proc = subprocess.run([sys.executable, str(guard)], input=json.dumps(event).encode("utf-8"),
                          capture_output=True, cwd=REPO, timeout=30, check=False,
                          env={**os.environ, "LAOP_GUARD_SELFTEST": "1"})
    return proc.returncode, proc.stderr.decode("utf-8", "replace").strip()


def probe_path(pattern: str) -> str:
    """A concrete path matching an allow_write glob."""
    parts = []
    for part in pattern.split("/"):
        parts.append("selftest" if part == "**" else part.replace("*", "selftest"))
    path = "/".join(parts)
    return path if "." in path.rsplit("/", 1)[-1] else path + "/_selftest_probe.txt"


def cases() -> list[tuple[str, dict, int, str]]:
    import pathrules

    roles = pathrules.load_roles(REPO)
    out = []
    loop_roles = sorted(pathrules.dispatch_roles(REPO))
    if loop_roles:
        role = loop_roles[0]
        cfg = roles[role]
        agent = {"agent_type": role, "agent_id": "selftest"}
        if not pathrules.matches_any("CLAUDE.md", cfg.get("allow_write", [])):
            out.append((f"{role} writes CLAUDE.md", {"tool_name": "Write", "tool_input":
                        {"file_path": "CLAUDE.md"}, **agent}, 2, "Write blocked"))
        for pattern in cfg.get("allow_write", []):
            probe = probe_path(pattern)
            if pathrules.check_write(role, probe, REPO)[0]:
                out.append((f"{role} writes {probe}", {"tool_name": "Write", "tool_input":
                            {"file_path": probe}, **agent}, 0, ""))
                break
    for protected in pathrules.load_project(REPO)["protected_paths"]:
        target = str((REPO / protected).resolve() / "_selftest_probe.txt")
        out.append((f"owner writes into {protected}", {"tool_name": "Write", "tool_input":
                    {"file_path": target}}, 2, "protected path"))
    return out


def core_drift() -> list[str]:
    """Core or rendered files whose content differs from harness/core.lock.json."""
    if not LOCK_FILE.exists():
        return ["harness/core.lock.json missing"]
    lock = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
    drift = []
    for entry, digest in sorted(lock.get("files", {}).items()):
        if lock_digest(entry) != digest:
            drift.append(entry)
    return drift


def lock_digest(entry: str) -> str:
    """Digest of a locked entry: a whole file (line endings normalised), or ``file#key``, the
    canonical JSON of one top-level key of a JSON file (``.claude/settings.json#hooks``)."""
    rel, _, key = entry.partition("#")
    path = REPO / rel
    if not path.exists():
        return "missing"
    if key:
        try:
            value = json.loads(path.read_text(encoding="utf-8-sig")).get(key)
        except (ValueError, AttributeError):
            return "invalid"
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def selftest() -> int:
    import loop_config as C

    for name, event, want_code, want_text in cases():
        code, err = run_guard(event)
        # The guard fails closed (exit 2) on internal errors too: require the real message.
        if code != want_code or (want_text and want_text not in err):
            print(f"{C.NAME} guard self-test FAILED ({name}: exit {code}, expected {want_code}): "
                  f"protections are OFF or broken. {err[:300]}", file=sys.stderr)
            return 2
    drift = core_drift()
    version = json.loads(LOCK_FILE.read_text(encoding="utf-8")).get("version", "?") if LOCK_FILE.exists() else "?"
    message = f"{C.NAME} guard active (laop {version}). Python: {sys.executable}"
    if drift:
        message += (f". WARNING: core files modified locally: {', '.join(drift[:6])}"
                    f"{' ...' if len(drift) > 6 else ''}. Run '/laop doctor'.")
    print(json.dumps({"systemMessage": message}))
    return 0


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
    try:
        return selftest()
    except Exception as exc:  # noqa: BLE001 - report every failure to the owner
        print(f"Guard self-test FAILED ({type(exc).__name__}: {exc}): protections may be OFF.",
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
