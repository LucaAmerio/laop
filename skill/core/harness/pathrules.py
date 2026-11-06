"""Path permission rules shared by the guard hook and the step scripts (core file: do not edit).

The role of a main conversation follows its session: ``orchestrator`` for the session bound to
the loop in harness/state.json (the guard binds the session that runs ``harness/step_next.py``;
a prompt typed by the owner in that session releases it, harness/prompt_hook.py), ``owner`` for
every other session. Inside a subagent the role is the subagent's name when it is a role.

Roles come from two places:

- the core roles defined here (``owner``, ``orchestrator``, ``resolver``, ``challenger``), which
  the project cannot redefine;
- the project roles of ``harness/roles.json`` (config).

Path rules of the project (``harness/project.json``): ``lockable`` directories whose marked
subdirectories are read-only for every role but owner, and ``protected_paths`` outside the
repository that every role, owner included, may read but never write.

Patterns are repository-relative POSIX globs: ``*`` matches inside one path component, ``**``
across components, ``**/`` zero or more directories. Both enforcement layers use this module:
``.claude/hooks/guard.py`` before every tool call, ``harness/step_verify.py`` on the git diff of
every task (backstop for writes the hook cannot see, e.g. files written by Python code).
"""

from __future__ import annotations

import codecs
import json
import os
import re
import tempfile
from functools import lru_cache
from pathlib import Path

ROLES_FILE_REL = "harness/roles.json"
STATE_FILE_REL = "harness/state.json"
PROJECT_FILE_REL = "harness/project.json"
OWNER_ROLE = "owner"
ORCHESTRATOR_ROLE = "orchestrator"
RESOLVER_ROLES = ("resolver", "challenger")

_READ_ONLY_GIT = ["status", "log", "diff", "show", "rev-parse", "ls-files", "grep", "blame"]
CORE_ROLES: dict[str, dict] = {
    OWNER_ROLE: {"allow_write": ["**"]},
    ORCHESTRATOR_ROLE: {
        "allow_write": [],
        "bash_exact": [
            r"^(python3?|py|\.venv/bin/python3?|\.venv/Scripts/python(\.exe)?)"
            r" harness/step_(next|verify|record|resolve)\.py$",
            r"^git status$",
            r"^git log --oneline( -n)? ?-?\d+$",
        ],
    },
    # deny_read of the resolver roles is the deny_read of the role whose question they answer
    # (dynamic, see role_config): a blind role's question is resolved without unblinding it.
    "resolver": {"allow_write": ["harness/resolutions/**"], "git_allow": _READ_ONLY_GIT},
    "challenger": {"allow_write": ["harness/resolutions/**"], "git_allow": _READ_ONLY_GIT},
}

# Windows file systems are case-insensitive and ignore trailing dots and spaces of path
# components; "name:stream" addresses an NTFS alternate data stream of "name".
WINDOWS = os.name == "nt"
_MSYS_DRIVE = re.compile(r"^/(?:cygdrive/|mnt/)?([A-Za-z])(?=/|$)")
_BOMS = (
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)


class RulesError(ValueError):
    """A configuration or state file the rules depend on is unreadable (callers fail closed)."""


def decode_text(data: bytes) -> str:
    """Decode a small text file written by any Windows or POSIX tool.

    Recognises UTF-8 (with or without BOM), UTF-16 and UTF-32 with BOM (Windows PowerShell 5.1
    ``Out-File`` and ``>`` write UTF-16 LE with BOM) and BOM-less UTF-16 (detected from NUL
    bytes). Raises ``UnicodeDecodeError`` for anything else, so that callers fail closed.
    """
    for bom, encoding in _BOMS:
        if data.startswith(bom):
            return data.decode(encoding)
    if len(data) >= 2 and b"\x00" in data:
        return data.decode("utf-16-be" if data[0] == 0 else "utf-16-le")
    return data.decode("utf-8")


def _read_json(path: Path, what: str):
    try:
        return json.loads(decode_text(path.read_bytes()))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RulesError(f"{what} is not valid JSON ({exc}). Tell the owner.") from exc


# --------------------------------------------------------------------------- globs


@lru_cache(maxsize=None)
def _compile(pattern: str) -> re.Pattern[str]:
    """Translate a repository glob into a compiled regular expression."""
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def glob_match(rel_path: str, pattern: str) -> bool:
    """Return True if a repository-relative POSIX path matches a glob pattern."""
    return bool(_compile(pattern).match(rel_path))


def matches_any(rel_path: str, patterns: list[str]) -> bool:
    """Return True if the path matches at least one of the patterns."""
    return any(glob_match(rel_path, p) for p in patterns)


def could_contain(rel_dir: str, pattern: str) -> bool:
    """True if a path matching ``pattern`` may lie inside the directory ``rel_dir``.

    ``rel_dir`` is "." for the repository root. Compared component by component; a ``**``
    component matches any depth.
    """
    dir_parts = [] if rel_dir in (".", "") else rel_dir.split("/")
    pat_parts = pattern.split("/")
    for i, part in enumerate(dir_parts):
        if i >= len(pat_parts):
            return True  # the directory is inside a matched path
        if pat_parts[i] == "**":
            return True
        if not glob_match(part, pat_parts[i]):
            return False
    return len(pat_parts) > len(dir_parts)


# --------------------------------------------------------------------------- configuration


def load_roles(repo: Path) -> dict[str, dict]:
    """All roles: the core ones, then the project's (which may not redefine a core role)."""
    data = _read_json(repo / ROLES_FILE_REL, ROLES_FILE_REL)
    project = data.get("roles", {}) if isinstance(data, dict) else {}
    clash = sorted(set(project) & set(CORE_ROLES))
    if clash:
        raise RulesError(f"{ROLES_FILE_REL} redefines core roles {clash}; remove them.")
    return {**CORE_ROLES, **project}


def load_project(repo: Path) -> dict:
    """Path settings of harness/project.json (missing file: none)."""
    path = repo / PROJECT_FILE_REL
    data = _read_json(path, PROJECT_FILE_REL) if path.exists() else {}
    return {"lockable": data.get("lockable", []), "protected_paths": data.get("protected_paths", [])}


def read_state(repo: Path) -> dict:
    """harness/state.json, or {} when missing. Raises RulesError when unreadable."""
    path = repo / STATE_FILE_REL
    if not path.exists():
        return {}
    state = _read_json(path, STATE_FILE_REL)
    return state if isinstance(state, dict) else {}


def dispatch_roles(repo: Path) -> set[str]:
    """Project roles the loop may dispatch for a task."""
    return {n for n, c in load_roles(repo).items() if c.get("dispatch") and n not in CORE_ROLES}


def reviewer_roles(repo: Path) -> set[str]:
    """Project roles that review a task."""
    return {n for n, c in load_roles(repo).items() if c.get("reviewer") and n not in CORE_ROLES}


# --------------------------------------------------------------------------- roles


def orchestrator_session(repo: Path) -> str | None:
    """Session id bound as the loop orchestrator in harness/state.json, or None."""
    return read_state(repo).get("orchestrator_session") or None


def main_role(repo: Path, session_id: str | None = None) -> str:
    """Role of a main conversation: ``orchestrator`` for the bound session, else ``owner``."""
    bound = orchestrator_session(repo)
    if bound and session_id and session_id == bound:
        return ORCHESTRATOR_ROLE
    return OWNER_ROLE


def effective_role(repo: Path, agent_type: str | None, session_id: str | None = None) -> str:
    """Role of the context that issued a tool call.

    A subagent whose name is a role (other than owner and orchestrator) gets that role; any other
    agent type (Explore, general-purpose, ...) inherits the role of its main conversation, so it
    can never exceed the permissions of the session that spawned it.
    """
    return role_resolution(repo, agent_type, session_id)[0]


def role_resolution(repo: Path, agent_type: str | None, session_id: str | None = None,
                    agent_id: str | None = None) -> tuple[str, str]:
    """Return ``effective_role`` and how it was resolved, for the guard's blocks log.

    - ``subagent``: ``agent_type`` names a role and the call comes from a subagent (``agent_id``);
    - ``agent_type_without_agent_id``: ``agent_type`` names a role but the hook input has no
      ``agent_id``: a main session started as that agent (``claude --agent <role>``, or the
      ``agent`` setting), not a subagent the loop spawned. It gets that role all the same (never
      more permissions than the role), and the log says so;
    - ``orchestrator_binding``: a main conversation bound as the orchestrator in state.json;
    - ``owner_default``: any other main conversation.
    """
    roles = load_roles(repo)
    if agent_type and agent_type in roles and agent_type not in (OWNER_ROLE, ORCHESTRATOR_ROLE):
        return agent_type, "subagent" if agent_id else "agent_type_without_agent_id"
    role = main_role(repo, session_id)
    return role, "orchestrator_binding" if role == ORCHESTRATOR_ROLE else "owner_default"


def role_config(repo: Path, role: str | None) -> tuple[str, dict | None]:
    """Return the role name and its configuration (None if unknown: callers fail closed).

    The resolver roles inherit the ``deny_read`` of the role whose question they handle (the
    agent of the task in progress).
    """
    name = role or ""
    cfg = load_roles(repo).get(name)
    if cfg is not None and name in RESOLVER_ROLES:
        current = read_state(repo).get("current") or {}
        blind = load_roles(repo).get(current.get("agent", ""), {})
        cfg = {**cfg, "deny_read": list(blind.get("deny_read", []))}
    return name, cfg


# --------------------------------------------------------------------------- paths


def msys_to_windows(path: str | os.PathLike) -> str | None:
    """Translate a Git Bash / Cygwin path to its Windows form (Windows only).

    ``/c/Users/x`` and ``/cygdrive/c/Users/x`` become ``C:/Users/x``; ``/tmp/x`` becomes
    ``<system temp>/x``. Returns None when the path has no such form or not on Windows.
    """
    if not WINDOWS:
        return None
    s = os.fspath(path).replace("\\", "/")
    m = _MSYS_DRIVE.match(s)
    if m:
        return f"{m.group(1).upper()}:/" + s[m.end():].lstrip("/")
    if s == "/tmp" or s.startswith("/tmp/"):
        return tempfile.gettempdir() + s[4:]
    return None


def interpretations(path: str | os.PathLike) -> list[str]:
    """All the files a path string may denote: itself and, on Windows, its MSYS translation.

    The checks below allow an access only if every interpretation is allowed.
    """
    literal = os.fspath(path)
    alt = msys_to_windows(literal)
    return [literal] if alt is None or alt == literal else [literal, alt]


def _absolute(path: str | os.PathLike, repo: Path, cwd: str | os.PathLike | None) -> str:
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = Path(cwd) / p if cwd else repo / p
    return os.path.realpath(p)


def _clean_component(part: str) -> str:
    """Name that Windows actually opens for a path component (no stream, no trailing dots)."""
    if part in (".", ".."):
        return part
    return part.split(":", 1)[0].rstrip(" .") or part


def to_repo_relative(path, repo: Path, cwd=None) -> str | None:
    """Return the repository-relative POSIX path, or None if outside the repository.

    The repository root itself is returned as ".". On Windows each component is reduced to the
    name the file system opens: ``a.md.``, ``a.md `` and ``a.md:stream`` all become ``a.md``.
    """
    real = _absolute(path, repo, cwd)
    try:
        rel = Path(real).relative_to(os.path.realpath(repo))
    except ValueError:
        return None
    posix = rel.as_posix()
    if WINDOWS and posix != ".":
        posix = "/".join(_clean_component(p) for p in posix.split("/"))
    return posix


def _within(real: str, root: str) -> bool:
    real, root = os.path.normcase(real), os.path.normcase(root)
    return real == root or real.startswith(root.rstrip(os.sep) + os.sep)


def in_protected_path(path, repo: Path, cwd=None) -> str | None:
    """The configured protected path containing ``path``, or None."""
    real = _absolute(path, repo, cwd)
    for p in load_project(repo)["protected_paths"]:
        root = _absolute(p, repo, None)
        if _within(real, root):
            return p
    return None


def _is_temp(path, repo: Path, cwd=None) -> bool:
    return _within(_absolute(path, repo, cwd), os.path.realpath(tempfile.gettempdir()))


def _deny_match(rel: str, patterns: list[str]) -> bool:
    """Case-insensitive match for deny rules (fail closed on case-insensitive file systems)."""
    return matches_any(rel.lower(), [p.lower() for p in patterns])


def _locked(rel: str, repo: Path) -> str | None:
    """The locked subdirectory containing ``rel`` (lockable directories of the config), or None."""
    for entry in load_project(repo)["lockable"]:
        base = entry["dir"].strip("/")
        marker = entry.get("marker", "LOCKED")
        if rel.lower().startswith(base.lower() + "/"):
            parts = rel[len(base) + 1:].split("/")
            if len(parts) >= 2 and (repo / base / parts[0] / marker).exists():
                return f"{base}/{parts[0]}"
    return None


def _markers(repo: Path) -> set[str]:
    return {e.get("marker", "LOCKED").lower() for e in load_project(repo)["lockable"]}


# --------------------------------------------------------------------------- checks


def check_write(role: str | None, path, repo: Path, cwd=None) -> tuple[bool, str]:
    """Decide whether a role may create, modify or delete a path.

    Every interpretation of the path must be allowed. Returns ``(allowed, reason)``; the reason
    is written for the agent.
    """
    for candidate in interpretations(path):
        ok, why = _check_write_one(role, candidate, repo, cwd)
        if not ok:
            return ok, why
    return True, ""


def _check_write_one(role: str | None, path, repo: Path, cwd=None) -> tuple[bool, str]:
    name, cfg = role_config(repo, role)
    if cfg is None:
        return False, f"Unknown role '{name}'. Check harness/roles.json."
    protected = in_protected_path(path, repo, cwd)
    if protected:
        return False, (f"'{path}' is inside the protected path '{protected}', which is read-only "
                       "for every role (harness/project.json, protected_paths).")
    rel = to_repo_relative(path, repo, cwd)
    if rel is None:
        if name == OWNER_ROLE or _is_temp(path, repo, cwd):
            return True, ""
        return False, f"'{path}' is outside the repository and outside the temp directory."
    if name == OWNER_ROLE:
        return True, ""
    if rel.split("/")[-1].lower() in _markers(repo):
        return False, "Only the owner creates or removes lock markers."
    locked = _locked(rel, repo)
    if locked:
        return False, f"'{rel}' belongs to '{locked}', locked by the owner: read-only."
    if _deny_match(rel, cfg.get("deny_write", [])):
        return False, f"'{rel}' is explicitly write-protected for role '{name}'."
    if not matches_any(rel, cfg.get("allow_write", [])):
        return False, f"'{rel}' is outside the write allowlist of role '{name}'."
    return True, ""


def check_read(role: str | None, path, repo: Path, cwd=None, recursive: bool = False) -> tuple[bool, str]:
    """Decide whether a role may read a path (independence rules of ``deny_read``).

    ``recursive``: the caller reads every file under the path when it is a directory (Grep, a
    recursive grep); then a directory that may contain a hidden file is refused too.
    """
    for candidate in interpretations(path):
        ok, why = _check_read_one(role, candidate, repo, cwd, recursive)
        if not ok:
            return ok, why
    return True, ""


def _check_read_one(role, path, repo: Path, cwd, recursive: bool) -> tuple[bool, str]:
    name, cfg = role_config(repo, role)
    if cfg is None:
        return False, f"Unknown role '{name}'. Check harness/roles.json."
    deny = cfg.get("deny_read", [])
    if not deny:
        return True, ""
    rel = to_repo_relative(path, repo, cwd)
    if rel is None:
        return True, ""
    if _deny_match(rel, deny):
        return False, (f"Role '{name}' may not read '{rel}': this file is hidden to keep your work "
                       "independent. Do not try to access it by other means.")
    is_dir = rel == "." or (repo / rel).is_dir()
    if recursive and is_dir and any(could_contain(rel.lower(), p.lower()) for p in deny):
        return False, (f"Role '{name}' may not search '{rel}' recursively: it contains files hidden "
                       "from this role. Search a narrower directory that holds none of them.")
    return True, ""


def literal_basenames(patterns: list[str]) -> list[str]:
    """Return the wildcard-free last components of patterns (e.g. 'derivation.md')."""
    names = []
    for p in patterns:
        last = p.rsplit("/", 1)[-1]
        if last and not any(ch in last for ch in "*?"):
            names.append(last)
    return names
