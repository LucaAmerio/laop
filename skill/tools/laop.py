"""Installer of the laop task-loop harness: the deterministic part of the /laop skill.

Usage (the skill runs these; the owner may too):

    python <skill>/tools/laop.py init    <repo> <answers.json>   install into a repository
    python <skill>/tools/laop.py upgrade <repo> [--force]        replace the core with this version
    python <skill>/tools/laop.py doctor  <repo>                  check an installed harness
    python <skill>/tools/laop.py render  <repo>                  re-render the rendered files

Classes of files: core files are copied from ``core/`` and never edited in the
project; rendered files are generated from ``render/`` and ``harness/project.json``; config files
are generated once by ``init`` from the interview answers and belong to the owner afterwards.
``harness/core.lock.json`` records the version and the digests of core and rendered files.
Standard library only.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
CORE = SKILL / "core"
RENDER = SKILL / "render"
TEMPLATES = SKILL / "templates"
VERSION = (CORE / "VERSION").read_text(encoding="utf-8").strip()
LOCK_REL = "harness/core.lock.json"
SETTINGS_REL = ".claude/settings.json"

GITIGNORE_BLOCK = """
# laop task-loop runtime files (harness/README.md)
harness/state.json
harness/STATUS.md
harness/claim.json
harness/NEXT_FINDINGS.md
harness/findings/
harness/STEER.md
harness/STEER_*.md
harness/*_prompt.md
harness/owner_queue.json
harness/owner_writes.json
harness/verdicts/
harness/resolutions/
harness/screenshots/
harness/logs/
harness/*.lock
harness/*.tmp
AGENT_STOP
AGENT_SOFT_STOP
""".strip()

#: What the analyst writes and the planner must obey (harness/approvals.py).
ANALYST_DOCS = ["docs/spec.md", "docs/plan/roadmap.md", "docs/plan/*.brief.md"]
#: The loop command, the same in every project (core/harness/loop_config.py LOOP_COMMAND).
LOOP_COMMAND = "laop-start"
#: Agents written by init (templates/agents/<name>.md).
INIT_AGENTS = ("analyst", "spec-reviewer", "planner", "builder", "evaluator")

DEFAULTS = {
    "owner_language": "English",
    "purpose": "",
    "owner_profile": "",
    "platform": "windows",
    "interpreters": {"python": [".venv/Scripts/python.exe", ".venv/bin/python"]},
    "global_checks": [],
    "open_questions": "docs/open_questions.md",
    "builder_paths": ["src/**", "tests/**", "docs/adr/**"],
    "builder_deny": [],
    "lockable": [],
    "protected_paths": [],
    "external_programs": [],
    "integrity_paths": [],
    "ntfy_topic": None,
    "resolver_mode": "advise",
    "north_star": "(to be written by the owner)",
    "principles": [],
    "owner_knows": "(to be written by the owner)",
    "owner_delegates": "(to be written by the owner)",
    "conventions": "(to be written by the owner)",
    "extra_roles": {},
}


# --------------------------------------------------------------------------- helpers


def digest_file(path: Path) -> str:
    if not path.exists():
        return "missing"
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def digest_entry(repo: Path, entry: str) -> str:
    """Same digest as harness/session_check.py: a file, or ``file#key`` of a JSON file."""
    rel, _, key = entry.partition("#")
    path = repo / rel
    if not key:
        return digest_file(path)
    if not path.exists():
        return "missing"
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig")).get(key)
    except (ValueError, AttributeError):
        return "invalid"
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def core_files() -> list[str]:
    """Repository-relative paths of the core files (everything under core/ but VERSION)."""
    return sorted(p.relative_to(CORE).as_posix() for p in CORE.rglob("*")
                  if p.is_file() and p.name != "VERSION" and "__pycache__" not in p.parts)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def fill(text: str, values: dict[str, str]) -> str:
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def leftovers(text: str) -> list[str]:
    import re
    return sorted(set(re.findall(r"\{\{([A-Z_]+)\}\}", text)))


def load_project(repo: Path) -> dict:
    path = repo / "harness" / "project.json"
    return json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}


# --------------------------------------------------------------------------- rendered files


def hooks_block(hook_python: list[str]) -> dict:
    def cmd(script: str, timeout: int = 30) -> dict:
        return {"type": "command", "command": hook_python[0],
                "args": [*hook_python[1:], "${CLAUDE_PROJECT_DIR}/" + script], "timeout": timeout}

    return {
        "SessionStart": [{"hooks": [cmd("harness/session_check.py")]}],
        "PreToolUse": [{"matcher": "Write|Edit|MultiEdit|NotebookEdit|Read|Grep|Glob|Bash|PowerShell|Agent|Task",
                        "hooks": [cmd(".claude/hooks/guard.py")]}],
        "UserPromptSubmit": [{"hooks": [cmd("harness/prompt_hook.py")]}],
        "Stop": [{"hooks": [cmd("harness/stop_hook.py")]}],
        "Notification": [{"matcher": "permission_prompt|idle_prompt|agent_needs_input",
                          "hooks": [cmd("harness/notify_hook.py", 20)]}],
        "StopFailure": [{"hooks": [cmd("harness/notify_hook.py", 20)]}],
    }


DEFAULT_SETTINGS = {
    "env": {"CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1", "CLAUDE_CODE_FORK_SUBAGENT": "0"},
    "permissions": {
        "allow": [
            "Bash(python harness/step_next.py)", "Bash(python harness/step_verify.py)",
            "Bash(python harness/step_record.py)", "Bash(python harness/step_resolve.py)",
            "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)", "Bash(git show:*)",
            "Bash(git add:*)", "Bash(git commit:*)", "Bash(git rev-parse:*)", "Bash(git ls-files:*)",
            "Bash(python:*)", "Bash(py:*)", "Bash(.venv/Scripts/python.exe:*)", "Bash(.venv/bin/python:*)",
            "Bash(ls:*)", "Bash(cat:*)", "Bash(head:*)", "Bash(tail:*)", "Bash(grep:*)", "Bash(mkdir:*)",
        ],
        "ask": ["Bash(git push:*)", "Bash(git reset:*)", "Bash(git rebase:*)", "Bash(git checkout:*)",
                "Bash(git restore:*)", "Bash(git clean:*)", "Bash(git stash:*)", "Bash(git branch -D:*)"],
        "deny": ["Bash(pip:*)", "Bash(pip3:*)", "Bash(python -m pip:*)", "Bash(uv:*)", "Bash(conda:*)",
                 "Agent(fork)"],
    },
}


def render(repo: Path) -> list[str]:
    """Write the rendered files from project.json; return their lock entries."""
    project = load_project(repo)
    name = project.get("name", "Project")
    text = fill((RENDER / "loop-command.md").read_text(encoding="utf-8"),
                {"NAME": name, "OWNER_LANGUAGE": project.get("owner_language", "English")})
    cmd_rel = f".claude/commands/{LOOP_COMMAND}.md"
    write_text(repo / cmd_rel, text)
    settings_path = repo / SETTINGS_REL
    settings = json.loads(settings_path.read_text(encoding="utf-8-sig")) if settings_path.exists() \
        else json.loads(json.dumps(DEFAULT_SETTINGS))
    settings["hooks"] = hooks_block(project.get("hook_python", ["py", "-3"]))
    write_text(settings_path, json.dumps(settings, indent=2) + "\n")
    return [cmd_rel, f"{SETTINGS_REL}#hooks"]


def write_lock(repo: Path, entries: list[str]) -> None:
    lock = {"version": VERSION, "files": {e: digest_entry(repo, e) for e in sorted(entries)}}
    write_text(repo / LOCK_REL, json.dumps(lock, indent=2) + "\n")


def lock_entries(repo: Path) -> list[str]:
    path = repo / LOCK_REL
    return list(json.loads(path.read_text(encoding="utf-8"))["files"]) if path.exists() else []


def upgrade_pending(lock: dict) -> list[str]:
    """Core files of this skill that differ from the ones installed (by the lock's digests): an
    upgrade has something to do even when the version is the same (unreleased changes)."""
    return [f for f in core_files() if lock["files"].get(f) != digest_file(CORE / f)]


def drift(repo: Path) -> list[str]:
    path = repo / LOCK_REL
    if not path.exists():
        return []
    lock = json.loads(path.read_text(encoding="utf-8"))
    return [e for e, d in sorted(lock["files"].items()) if digest_entry(repo, e) != d]


# --------------------------------------------------------------------------- config generation


def slug(name: str) -> str:
    return "".join(c.lower() if c.isalnum() else "-" for c in name).strip("-") or "project"


def project_json(a: dict) -> dict:
    hook_python = ["py", "-3"] if a["platform"] == "windows" else ["python3"]
    return {
        "_doc": "Project settings of the task loop (config, owned by the owner). Missing keys take the "
                "defaults of harness/loop_config.py. See harness/README.md.",
        "name": a["name"],
        "prefix": a.get("prefix") or slug(a["name"]).upper().replace("-", "_"),
        "owner_language": a["owner_language"],
        "interpreters": a["interpreters"],
        "hook_python": a.get("hook_python") or hook_python,
        "global_checks": a["global_checks"],
        "check_timeout_min": 9,
        "check_budget_min": 9.5,
        "max_iterations": 100,
        "max_attempts_per_task": 3,
        "max_stops_without_progress": 2,
        "max_empty_runs": 2,
        "protected_branches": ["main", "master"],
        "ntfy_topic": a["ntfy_topic"],
        "ntfy_verbosity": "important",
        "open_questions": a["open_questions"],
        "principles_file": "docs/owner_principles.md",
        "lockable": a["lockable"],
        "protected_paths": a["protected_paths"],
        "external_programs": a["external_programs"],
        "integrity_paths": a["integrity_paths"],
        "require_brief": a.get("require_brief", True),
        "resolver": {"mode": a.get("resolver_mode") or ("auto" if a.get("resolver_enabled") else "advise"),
                     "max_per_run": 5, "max_per_task": 2, "warmup": 3},
    }


def roles_json(a: dict) -> dict:
    oq = a["open_questions"]
    read_git = ["status", "log", "diff", "show", "rev-parse", "ls-files"]
    roles = {
        "analyst": {"allow_write": [*ANALYST_DOCS, oq], "git_allow": [*read_git, "add", "commit"]},
        "spec-reviewer": {"allow_write": ["docs/plan/reviews/**"], "git_allow": read_git},
        "planner": {"allow_write": ["harness/tasks.json", "docs/plan/**", oq], "deny_write": ANALYST_DOCS,
                    "git_allow": [*read_git, "add", "commit"]},
        "builder": {"dispatch": True,
                    "allow_write": [*a["builder_paths"], oq, "harness/claim.json", "harness/PROGRESS.md"],
                    "git_allow": [*read_git, "add", "commit", "rm", "mv"]},
        "evaluator": {"reviewer": True, "allow_write": ["harness/verdicts/**"], "git_allow": read_git},
    }
    if a["builder_deny"]:
        roles["builder"]["deny_write"] = a["builder_deny"]
    roles.update(a["extra_roles"])
    return {"_doc": "Project roles (config, owned by the owner). Core roles (owner, orchestrator, resolver, "
                    "challenger) are defined by the harness and cannot appear here. See harness/README.md.",
            "roles": roles}


def template_values(a: dict) -> dict[str, str]:
    principles = "\n".join(f"- **N{i}.** {p}" for i, p in enumerate(a["principles"], 1)) \
        or "(to be written by the owner)"
    py = a["interpreters"].get("python", [])
    interp = (f"- Project interpreter: {', '.join(f'`{p}`' for p in py)} (first existing). Run the checks "
              "through it.") if py else ""
    return {
        "NAME": a["name"], "PURPOSE": a["purpose"], "OWNER_PROFILE": a["owner_profile"],
        "NORTH_STAR": a["north_star"], "PRINCIPLES": principles, "OWNER_KNOWS": a["owner_knows"],
        "OWNER_DELEGATES": a["owner_delegates"], "OPEN_QUESTIONS": a["open_questions"],
        "INTERPRETER_LINE": interp, "CONVENTIONS": a["conventions"],
        "BUILDER_OWNS": ", ".join(f"`{p}`" for p in a["builder_paths"]),
        "EVALUATOR_EXTRA": "", "RESERVED_EXTRA": "", "DELEGATED_EXTRA": "",
    }


# --------------------------------------------------------------------------- commands


def init(repo: Path, answers_path: Path) -> int:
    if (repo / LOCK_REL).exists():
        print(f"{repo} already has the harness ({LOCK_REL}); use 'upgrade'.")
        return 1
    if not (repo / ".git").exists():
        print(f"{repo} is not a git repository: run 'git init' first.")
        return 1
    answers = json.loads(answers_path.read_text(encoding="utf-8-sig"))
    a = {**DEFAULTS, **{k: v for k, v in answers.items() if not k.startswith("_")}}
    if a.pop("loop_command", None):
        print(f"answers: 'loop_command' ignored; the loop command is /{LOOP_COMMAND} in every project.")
    clashes = [rel for rel in core_files() if (repo / rel).exists()
               and digest_file(repo / rel) != digest_file(CORE / rel)]
    if clashes:
        print("These files exist and differ from the laop core; move them aside first:\n  "
              + "\n  ".join(clashes))
        return 1
    for rel in core_files():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(CORE / rel, repo / rel)

    written, kept, todo = [], [], {}
    values = template_values(a)

    def config(rel: str, text: str) -> None:
        path = repo / rel
        if path.exists():
            alt = path.with_name(path.stem + ".laop" + path.suffix)
            write_text(alt, text)
            kept.append(f"{rel} (exists; proposal written to {alt.relative_to(repo).as_posix()})")
        else:
            write_text(path, text)
            written.append(rel)
        if leftovers(text):
            todo[rel] = leftovers(text)

    config("harness/project.json", json.dumps(project_json(a), indent=2) + "\n")
    config("harness/roles.json", json.dumps(roles_json(a), indent=2) + "\n")
    config("CLAUDE.md", fill((TEMPLATES / "CLAUDE.md").read_text(encoding="utf-8"), values))
    config("docs/owner_principles.md",
           fill((TEMPLATES / "owner_principles.md").read_text(encoding="utf-8"), values))
    for agent in INIT_AGENTS:
        config(f".claude/agents/{agent}.md",
               fill((TEMPLATES / "agents" / f"{agent}.md").read_text(encoding="utf-8"), values))
    if not (repo / a["open_questions"]).exists():
        config(a["open_questions"], "# Open questions for the owner\n\nEach entry: id (Qn), status, raised "
               "by, question, options, recommendation, consequences, decision.\n")
    if not (repo / "harness/PROGRESS.md").exists():
        config("harness/PROGRESS.md", "# Progress log\n")
    gitignore = repo / ".gitignore"
    current = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    if "laop task-loop runtime files" not in current:
        write_text(gitignore, (current.rstrip() + "\n\n" if current.strip() else "") + GITIGNORE_BLOCK + "\n")
        written.append(".gitignore (runtime block)")

    entries = core_files() + render(repo)
    write_lock(repo, entries)
    print(f"laop {VERSION} installed in {repo}.")
    print("Config written:\n  " + "\n  ".join(written))
    if kept:
        print("Existing files kept:\n  " + "\n  ".join(kept))
    if todo:
        print("Placeholders left to fill:\n  " + "\n  ".join(f"{k}: {', '.join(v)}" for k, v in todo.items()))
    return 0


def ensure_gitignore(repo: Path) -> list[str]:
    """Append the runtime entries of GITIGNORE_BLOCK missing from .gitignore; return them."""
    gitignore = repo / ".gitignore"
    current = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    present = {line.strip() for line in current.splitlines()}
    missing = [line for line in GITIGNORE_BLOCK.splitlines()[1:] if line.strip() and line.strip() not in present]
    if missing:
        nl = "\n"
        header = "" if "laop task-loop runtime files" in current else GITIGNORE_BLOCK.splitlines()[0] + nl
        write_text(gitignore, current.rstrip() + nl + header + nl.join(missing) + nl)
    return missing


def upgrade(repo: Path, force: bool) -> int:
    lock_path = repo / LOCK_REL
    if not lock_path.exists():
        print(f"{repo} has no {LOCK_REL}: use 'init'.")
        return 1
    old = json.loads(lock_path.read_text(encoding="utf-8"))
    modified = drift(repo)
    if modified and not force:
        print("Core or rendered files were modified locally; upgrade refused (rerun with --force to "
              "overwrite them):\n  " + "\n  ".join(modified))
        return 1
    new_core = core_files()
    changed = [rel for rel in new_core if digest_file(repo / rel) != digest_file(CORE / rel)]
    removed = [e for e in old["files"] if "#" not in e and not e.startswith(".claude/commands/")
               and e not in new_core]
    for rel in new_core:
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(CORE / rel, repo / rel)
    for rel in removed:
        (repo / rel).unlink(missing_ok=True)
    project_path = repo / "harness" / "project.json"
    project = load_project(repo)
    if "loop_command" in project:
        project.pop("loop_command")
        write_text(project_path, json.dumps(project, indent=2, ensure_ascii=False) + "\n")
    entries = new_core + render(repo)
    # the loop command used to be named after the project: drop the old rendered file
    for rel in old["files"]:
        if rel.startswith(".claude/commands/") and rel not in entries:
            (repo / rel).unlink(missing_ok=True)
            removed.append(rel)
    added = ensure_gitignore(repo)
    if added:
        print(".gitignore: added " + ", ".join(added))
    write_lock(repo, entries)
    print(f"laop {old.get('version', '?')} -> {VERSION} in {repo}.")
    print("Changed: " + (", ".join(changed) or "none"))
    print("Removed: " + (", ".join(removed) or "none"))
    return 0


def doctor(repo: Path) -> int:
    problems, notes = [], []
    if not (repo / LOCK_REL).exists():
        print("No harness/core.lock.json: the harness is not installed (use 'init').")
        return 1
    lock = json.loads((repo / LOCK_REL).read_text(encoding="utf-8"))
    notes.append(f"installed laop {lock.get('version')}, skill laop {VERSION}")
    stale = upgrade_pending(lock)
    if stale:
        notes.append(f"UPGRADE AVAILABLE: {len(stale)} core file(s) differ from this skill's "
                     f"({', '.join(stale[:5])}{', ...' if len(stale) > 5 else ''}); see /laop upgrade")
    for entry in drift(repo):
        problems.append(f"modified locally: {entry}")
    try:
        sys.path.insert(0, str(repo / "harness"))
        import pathrules  # noqa: PLC0415
        roles = pathrules.load_roles(repo)
        dispatch = pathrules.dispatch_roles(repo)
        reviewers = pathrules.reviewer_roles(repo)
        if not dispatch:
            problems.append("roles.json: no role with \"dispatch\": true")
        if "evaluator" not in reviewers:
            notes.append("roles.json: no 'evaluator' reviewer; every task must name its reviewer")
        for name in sorted(dispatch | reviewers | ({"analyst", "spec-reviewer"} & set(roles))):
            if not (repo / ".claude" / "agents" / f"{name}.md").exists():
                problems.append(f"role '{name}' has no agent file .claude/agents/{name}.md")
        resolver = load_project(repo).get("resolver") or {}
        mode = resolver.get("mode") or ("auto" if resolver.get("enabled") else "off")
        principles = repo / load_project(repo).get("principles_file", "docs/owner_principles.md")
        text = principles.read_text(encoding="utf-8") if principles.exists() else ""
        if mode != "off" and (not text or "(to be written by the owner)" in text or "{{" in text):
            notes.append(f"resolver mode {mode}, but {principles.relative_to(repo).as_posix()} is missing or "
                         "has placeholders: the resolver stays inactive until it is filled")
        else:
            notes.append(f"resolver mode {mode}")
        for prog in load_project(repo).get("external_programs", []):
            for agent in prog.get("agents", []):
                if agent not in dispatch:
                    problems.append(f"external program {prog.get('name')}: agent '{agent}' is not a "
                                    "role with \"dispatch\": true")
        notes.append(f"roles: {', '.join(sorted(roles))}")
        if "analyst" not in roles:
            notes.append("no 'analyst' role: spec, roadmap and briefs are written by hand (see the skill's "
                         "CHANGELOG, 0.4.0, to add it)")
        import approvals  # noqa: PLC0415
        tasks_path = repo / "harness" / "tasks.json"
        milestone = json.loads(tasks_path.read_text(encoding="utf-8-sig")).get("milestone") \
            if tasks_path.exists() else None
        notes.append("approvals: " + approvals.report(repo, milestone).replace("\n", "; "))
        if not load_project(repo).get("require_brief"):
            notes.append("require_brief is off: the loop starts without approved spec, roadmap and brief "
                         "(lint still warns); set \"require_brief\": true in harness/project.json")
        elif approvals.status(repo, approvals.SPEC_REL) == "missing":
            notes.append("next: /laop discover (spec and roadmap), then /laop brief <milestone>")
    except Exception as exc:  # noqa: BLE001 - report, do not crash
        problems.append(f"roles.json or project.json invalid: {exc}")
    gitignore = (repo / ".gitignore").read_text(encoding="utf-8") if (repo / ".gitignore").exists() else ""
    if "harness/state.json" not in gitignore:
        problems.append(".gitignore lacks the laop runtime block")
    project = load_project(repo)
    hook_python = project.get("hook_python", ["py", "-3"])
    try:
        proc = subprocess.run([*hook_python, str(repo / "harness" / "session_check.py")], cwd=repo,
                              capture_output=True, text=True, timeout=60)
        if proc.returncode != 0 or "guard active" not in proc.stdout:
            problems.append(f"guard self-test failed: {(proc.stderr or proc.stdout).strip()[:300]}")
        else:
            notes.append(json.loads(proc.stdout)["systemMessage"])
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        problems.append(f"hook interpreter {hook_python} cannot run the self-test: {exc}")
    print("\n".join(f"ok: {n}" for n in notes))
    print("\n".join(f"PROBLEM: {p}" for p in problems) or "No problems found.")
    return 1 if problems else 0


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[0] not in ("init", "upgrade", "doctor", "render"):
        print(__doc__)
        return 2
    repo = Path(argv[1]).resolve()
    if argv[0] == "init":
        if len(argv) < 3:
            print("init needs <repo> <answers.json>")
            return 2
        return init(repo, Path(argv[2]))
    if argv[0] == "upgrade":
        return upgrade(repo, "--force" in argv)
    if argv[0] == "doctor":
        return doctor(repo)
    entries = [e for e in lock_entries(repo) if "#" not in e and not e.startswith(".claude/commands/")]
    write_lock(repo, entries + render(repo))
    print("rendered files written")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main(sys.argv[1:]))
