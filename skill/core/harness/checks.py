"""The harness checks of a task, run in a process of their own (core file: do not edit).

A Bash call of Claude Code lasts at most 10 minutes; a test suite may last longer. step_verify
therefore never runs the checks itself: it starts this module as a detached runner
(``python harness/checks.py <run_id>``) and waits for it in slices of ``check_wait_min``, printing
``CHECKS RUNNING`` and asking to be run again until the run is finished.

The run is recorded in ``harness/logs/checks/run.json`` (atomic writes under a lock):

- ``run_id``, ``task_id``, ``attempt``, ``head`` (HEAD when the run started), the checks;
- ``status``: ``starting`` (launched), ``running`` (the runner wrote its ``pid``), ``done``
  (``all_ok`` and ``report``) or ``crashed`` (``error``);
- ``reports``: one entry per check, written as soon as the check ends.

The runner touches ``<run_id>.beat`` every few seconds. A run is alive while its process exists and
the heartbeat is fresh. Whoever waits next (the same session, or a new one after Claude Code, the
machine or the runner stopped) reads the record: a finished run whose HEAD is still current is used
as it is; a run that is gone, crashed, or ran on another HEAD is started again (``check_restarts``).

Standard library only.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import loop_config as C  # noqa: E402
import loop_core as L  # noqa: E402
import statefile  # noqa: E402

RUN_DIR = L.LOG_DIR / "checks"
RECORD = RUN_DIR / "run.json"
RUNNER_LOG = RUN_DIR / "runner.log"
HEARTBEAT_S = 5
#: A heartbeat older than this means the runner is gone (also the launch grace period).
STALE_S = 45


def read() -> dict:
    """The record of the last run, or {} when there is none or it is unreadable."""
    try:
        return statefile.read(RECORD)
    except (OSError, ValueError):
        return {}


def _update(run_id: str, change) -> bool:
    """Apply ``change(record)`` if the record still belongs to ``run_id``."""
    with statefile.locked(RECORD):
        rec = read()
        if rec.get("run_id") != run_id:
            return False
        change(rec)
        statefile.write(RECORD, rec)
    return True


def heartbeat_file(run_id: str) -> Path:
    return RUN_DIR / f"{run_id}.beat"


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return kernel32.GetLastError() == 5  # access denied: it exists
        code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def alive(rec: dict) -> bool:
    """The runner of ``rec`` is working: its process exists and its heartbeat is fresh (or it was
    launched less than STALE_S seconds ago and has not started yet)."""
    if rec.get("status") not in ("starting", "running"):
        return False
    if not rec.get("pid"):
        return time.time() - rec.get("created", 0) < STALE_S
    try:
        fresh = time.time() - heartbeat_file(rec["run_id"]).stat().st_mtime < STALE_S
    except OSError:
        fresh = time.time() - (rec.get("started") or 0) < STALE_S
    return fresh and pid_alive(rec["pid"])


def stop(rec: dict) -> None:
    """Kill the runner of ``rec`` and its checks, if it is still working."""
    if rec.get("pid") and alive(rec):
        L.kill_tree(rec["pid"])


def start(task: dict, attempt: int) -> str:
    """Record a new run of the checks of ``task`` and launch its runner; return the run id."""
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    stop(read())
    run_id = f"{task['id']}-{attempt}-{secrets.token_hex(4)}"
    checks = C.GLOBAL_CHECKS + task["acceptance"]
    rec = {"run_id": run_id, "task_id": task["id"], "attempt": attempt, "head": L.head_sha(),
           "acceptance": task["acceptance"], "n_checks": len(checks), "status": "starting",
           "created": time.time(), "reports": []}
    with statefile.locked(RECORD):
        statefile.write(RECORD, rec)
    launch(run_id)
    return run_id


def launch(run_id: str) -> None:
    """Start the runner detached from the calling process: it must survive the end of the Bash
    call, of step_verify and of the Claude Code session."""
    cmd = [sys.executable, str(Path(__file__).resolve()), run_id]
    with open(RUNNER_LOG, "ab") as log_fh:
        kw = {"cwd": L.REPO, "stdin": subprocess.DEVNULL, "stdout": log_fh, "stderr": subprocess.STDOUT,
              "close_fds": True}
        if os.name == "nt":
            # no window; own process group; out of the job object of Claude Code when allowed
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            try:
                subprocess.Popen(cmd, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, **kw)
            except OSError:
                subprocess.Popen(cmd, creationflags=flags, **kw)
        else:
            subprocess.Popen(cmd, start_new_session=True, **kw)


def run(run_id: str) -> int:
    """The runner: run the checks of the recorded run and record each result."""
    rec = read()
    if rec.get("run_id") != run_id:
        return 1
    beat = heartbeat_file(run_id)
    done = threading.Event()

    def beat_loop() -> None:
        while not done.is_set():
            try:
                beat.touch()
            except OSError:
                pass
            done.wait(HEARTBEAT_S)

    threading.Thread(target=beat_loop, daemon=True).start()
    started = time.time()
    if not _update(run_id, lambda r: r.update(status="running", pid=os.getpid(), started=started)):
        return 1
    task = {"id": rec["task_id"], "acceptance": rec["acceptance"]}

    def on_report(i: int, ok: bool, report: str) -> None:
        _update(run_id, lambda r: r["reports"].append({"i": i, "ok": ok, "time": time.time()}))

    try:
        ok, report = L.run_checks(task, rec["attempt"], started, on_report)
        _update(run_id, lambda r: r.update(status="done", all_ok=ok, report=report, finished=time.time()))
    except Exception:  # noqa: BLE001 - recorded: the waiter starts the run again
        _update(run_id, lambda r: r.update(status="crashed", error=traceback.format_exc()[-3000:],
                                           finished=time.time()))
    finally:
        done.set()
        beat.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(run(sys.argv[1]) if len(sys.argv) == 2 else 2)
