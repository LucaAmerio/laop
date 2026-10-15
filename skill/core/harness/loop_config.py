"""Parameters of the task loop, read from harness/project.json (core file: do not edit).

The project's settings live in ``harness/project.json`` (config, owned by the owner); this module
loads them, applies the defaults below for missing keys, and exposes them as constants to the
other harness modules. Standard library only.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PROJECT_FILE = REPO / "harness" / "project.json"

DEFAULTS: dict = {
    "name": "Project",
    "prefix": "PROJECT",
    "owner_language": "English",
    # Placeholder in a cmd check -> candidate interpreter paths (repository-relative or
    # absolute, "~" expanded); the first existing one wins, else the placeholder itself on PATH.
    "interpreters": {
        "python": [".venv/Scripts/python.exe", ".venv/bin/python"],
    },
    "hook_python": ["py", "-3"],
    "global_checks": [],
    "check_timeout_min": 9,
    "check_budget_min": 9.5,
    "max_iterations": 100,
    "max_attempts_per_task": 3,
    "max_stops_without_progress": 2,
    "max_empty_runs": 2,
    "protected_branches": ["main", "master"],
    "ntfy_topic": None,
    "ntfy_verbosity": "important",
    "open_questions": "docs/open_questions.md",
    "principles_file": "docs/owner_principles.md",
    # Subdirectories of `dir` containing `marker` are read-only for every role except owner.
    "lockable": [],
    # Paths outside the repository that every role, owner included, may read but never write.
    "protected_paths": [],
    # {"name", "agents", "reminder"}: reminder printed while tasks of those agents are not done.
    "external_programs": [],
    # Git-ignored paths whose content must not change while a task runs (files or directories).
    "integrity_paths": [],
    # mode: "off" (questions go to the owner as written by the agent), "advise" (resolver and
    # challenger prepare a recommendation for every question; the owner decides every one), "auto"
    # (as advise, and the question is decided provisionally when its category of the principles
    # file is autonomous: delegated categories after a warm-up of `warmup` agreed answers).
    # Refuse to start the loop unless docs/spec.md, docs/plan/roadmap.md and the milestone's brief
    # are approved by the owner (harness/approvals.py). init sets it; upgraded projects opt in.
    "require_brief": False,
    "resolver": {"mode": "off", "max_per_run": 5, "max_per_task": 2, "warmup": 3},
}

RESOLVER_MODES = ("off", "advise", "auto")


class ConfigError(Exception):
    """harness/project.json is missing or invalid."""


def load_project() -> dict:
    """Return the project settings with defaults applied (missing file: defaults only)."""
    data: dict = {}
    if PROJECT_FILE.exists():
        try:
            data = json.loads(PROJECT_FILE.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"harness/project.json is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("harness/project.json must contain a JSON object.")
    merged = {**DEFAULTS, **{k: v for k, v in data.items() if not k.startswith("_")}}
    raw = data.get("resolver") or {}
    merged["resolver"] = {**DEFAULTS["resolver"], **raw}
    if "mode" not in raw and "enabled" in raw:
        # laop <= 0.2: {"enabled": true} meant the provisional answers of today's "auto" mode
        merged["resolver"]["mode"] = "auto" if raw["enabled"] else "off"
    return merged


def _resolve_interpreter(name: str, candidates: list[str]) -> str:
    for c in candidates:
        p = Path(c).expanduser()
        p = p if p.is_absolute() else REPO / p
        if p.exists():
            return str(p)
    if name == "python":
        return sys.executable
    return shutil.which(name) or name


P = load_project()

NAME: str = P["name"]
PREFIX: str = P["prefix"]
#: The loop command, the same in every project (.claude/commands/laop-start.md).
LOOP_COMMAND = "laop-start"
OWNER_LANGUAGE: str = P["owner_language"]
INTERPRETERS: dict[str, str] = {k: _resolve_interpreter(k, v) for k, v in P["interpreters"].items()}
PYTHON: str = INTERPRETERS.get("python", sys.executable)
GLOBAL_CHECKS: list[dict] = P["global_checks"]
CHECK_TIMEOUT_MIN: float = P["check_timeout_min"]
CHECK_BUDGET_MIN: float = P["check_budget_min"]
MAX_ITERATIONS: int = P["max_iterations"]
MAX_ATTEMPTS_PER_TASK: int = P["max_attempts_per_task"]
MAX_STOPS_WITHOUT_PROGRESS: int = P["max_stops_without_progress"]
MAX_EMPTY_RUNS: int = P["max_empty_runs"]
PROTECTED_BRANCHES: tuple[str, ...] = tuple(P["protected_branches"])
NTFY_TOPIC: str | None = P["ntfy_topic"]
NTFY_VERBOSITY: str = P["ntfy_verbosity"]
OPEN_QUESTIONS_REL: str = P["open_questions"]
PRINCIPLES_REL: str = P["principles_file"]
LOCKABLE: list[dict] = P["lockable"]
PROTECTED_PATHS: list[str] = P["protected_paths"]
EXTERNAL_PROGRAMS: list[dict] = P["external_programs"]
INTEGRITY_PATHS: list[str] = P["integrity_paths"]
REQUIRE_BRIEF: bool = bool(P["require_brief"])
_r = P["resolver"]
RESOLVER_MODE: str = _r["mode"]
if RESOLVER_MODE not in RESOLVER_MODES:
    raise ConfigError(f"resolver.mode must be one of {RESOLVER_MODES}, not {RESOLVER_MODE!r}")
RESOLVER_ENABLED: bool = RESOLVER_MODE != "off"
RESOLVER_WARMUP: int = int(_r.get("warmup", 3))
MAX_RESOLUTIONS_PER_RUN: int = P["resolver"]["max_per_run"]
MAX_RESOLUTIONS_PER_TASK: int = P["resolver"]["max_per_task"]
