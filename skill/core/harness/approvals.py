"""Owner approvals of the documents that precede the plan (core file: do not edit).

Three documents define what the loop builds, and only the owner approves them:

- ``docs/spec.md``: requirements of the project (``/laop discover``);
- ``docs/plan/roadmap.md``: the milestones, their content and order (``/laop discover``);
- ``docs/plan/<milestone>.brief.md``: what one milestone means to the owner (``/laop brief``).

An approval records the digest of the document in ``harness/approvals.json``, a file no loop role
may write. Editing the document afterwards makes the approval stale: the owner approves again.
Standard library only.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from pathlib import Path

APPROVALS_REL = "harness/approvals.json"
SPEC_REL = "docs/spec.md"
ROADMAP_REL = "docs/plan/roadmap.md"
NAMES = {"spec": SPEC_REL, "roadmap": ROADMAP_REL}

#: Text that must not survive into an approved document: an open clarification or a placeholder.
_UNRESOLVED = (re.compile(r"\[NEEDS CLARIFICATION[^\]]*\]"), re.compile(r"\{\{[A-Z_]+\}\}"))


class ApprovalError(ValueError):
    """The document cannot be approved (missing, or with unresolved items)."""


def brief_rel(milestone: str) -> str:
    return f"docs/plan/{milestone}.brief.md"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def unresolved(text: str) -> list[str]:
    """Open clarifications and placeholders left in a document."""
    return sorted({m.group(0) for rx in _UNRESOLVED for m in rx.finditer(text)})


def load(repo: Path) -> dict:
    path = repo / APPROVALS_REL
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    return data.get("approved", {}) if isinstance(data, dict) else {}


def status(repo: Path, rel: str) -> str:
    """``missing``, ``unapproved``, ``stale`` (edited since the approval) or ``approved``."""
    path = repo / rel
    if not path.exists():
        return "missing"
    entry = load(repo).get(rel)
    if not entry:
        return "unapproved"
    return "approved" if entry.get("sha256") == digest(path) else "stale"


def approve(repo: Path, rel: str) -> str:
    """Record the owner's approval of a document; return a message for the owner."""
    path = repo / rel
    if not path.exists():
        raise ApprovalError(f"{rel} does not exist.")
    left = unresolved(path.read_text(encoding="utf-8-sig"))
    if left:
        raise ApprovalError(f"{rel} still has unresolved items; resolve them first: " + "; ".join(left[:10]))
    approved = load(repo)
    approved[rel] = {"sha256": digest(path), "date": dt.date.today().isoformat()}
    doc = {"_doc": "Owner approvals of spec, roadmap and milestone briefs (written by "
                   "'owner_tools.py approve'; an edited document needs a new approval).",
           "approved": dict(sorted(approved.items()))}
    out = repo / APPROVALS_REL
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8", newline="\n")
    return f"{rel} approved. Commit {APPROVALS_REL} with it."


def milestone_problem(repo: Path, milestone: str) -> str:
    """'' when spec, roadmap and the milestone's brief are approved, otherwise what is missing."""
    problems = []
    for rel, how in ((SPEC_REL, "/laop discover"), (ROADMAP_REL, "/laop discover"),
                     (brief_rel(milestone), f"/laop brief {milestone}")):
        st = status(repo, rel)
        if st != "approved":
            problems.append(f"{rel} is {st}" + (f" (write it with {how})" if st == "missing" else
                                                f" (approve it: python harness/owner_tools.py approve doc={rel})"))
    return "; ".join(problems)


def report(repo: Path, milestone: str | None) -> str:
    rels = [SPEC_REL, ROADMAP_REL] + ([brief_rel(milestone)] if milestone else [])
    lines = [f"{rel}: {status(repo, rel)}" for rel in rels]
    others = sorted(p.relative_to(repo).as_posix() for p in (repo / "docs" / "plan").glob("*.brief.md")
                    if milestone is None or p.name != f"{milestone}.brief.md") \
        if (repo / "docs" / "plan").exists() else []
    lines += [f"{rel}: {status(repo, rel)}" for rel in others]
    return "\n".join(lines)
