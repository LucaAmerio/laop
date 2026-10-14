"""Locks and atomic writes for the loop state (core file: do not edit).

Several processes write ``harness/state.json``:

- the step scripts, which own almost every key and may run for minutes (step_verify runs the
  checks);
- the guard (``orchestrator_session``, when it binds a session), the UserPromptSubmit hook
  (``orchestrator_session``, when it releases it) and the Stop hook (``stop_guard``);
- owner_tools.py, run by the owner from another session while the loop runs.

Two locks keep them from losing each other's changes:

- the **state lock** (short): every read-modify-write of state.json holds it; a step script saving
  its state re-reads the keys written by the hooks (``EXTERNAL_KEYS``) under it and keeps them;
- the **step lock** (long): held by a step script for its whole run. Owner actions are appended to
  a queue (``owner_queue.json``); owner_tools applies the queue at once when no step runs, otherwise
  the next step applies it when it starts. An owner action is therefore never overwritten by a
  step that loaded the state before it.

Standard library only.
"""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

#: Keys of state.json written by the hooks, never by the step scripts.
EXTERNAL_KEYS = ("orchestrator_session", "stop_guard")


class LockBusy(RuntimeError):
    """The lock is held by another process."""


@contextmanager
def locked(path: Path, timeout: float = 20.0, stale: float = 120.0):
    """Hold the exclusive lock ``<path>.lock`` for the duration of the block.

    ``timeout`` 0 means do not wait (raise LockBusy at once). A lock file older than ``stale``
    seconds was left by a crashed process and is removed.
    """
    lock = path.with_name(path.name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"{os.getpid()} {time.time():.0f}".encode())
            os.close(fd)
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > stale:
                    lock.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise LockBusy(f"{lock.name} is held by another process") from None
            time.sleep(0.05)
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)


def read(path: Path) -> dict:
    """The JSON object stored at ``path``, or {} when the file does not exist."""
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} does not contain a JSON object")
    return data


def write(path: Path, data: dict) -> None:
    """Write ``data`` atomically (temporary file, then rename). Call it under ``locked``."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def update(path: Path, change) -> dict:
    """Apply ``change(state)`` to the stored state under the state lock, save it, return it."""
    with locked(path):
        state = read(path)
        change(state)
        write(path, state)
    return state


def save_owned(path: Path, state: dict) -> None:
    """Save a state loaded earlier by a step script, keeping the current EXTERNAL_KEYS on disk."""
    with locked(path):
        disk = read(path)
        for key in EXTERNAL_KEYS:
            if key in disk:
                state[key] = disk[key]
            else:
                state.pop(key, None)
        write(path, state)
