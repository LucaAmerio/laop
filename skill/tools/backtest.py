"""Backtest of the resolver on questions the owner already decided (standard library only).

Usage:

    python <skill>/tools/backtest.py prepare <repo> <out> [questions=loop|all|Q34,Q61] \
        [principles=<file>] [classification=<file>]
    python <skill>/tools/backtest.py report <out>

prepare
    For each selected question of the repository's open questions file:
    - a copy of the repository's tracked files at the commit that added the question (git archive:
      no git history, so later decisions cannot be read from it), with the owner's decision removed
      from the question entry;
    - the principles file (``principles=``, default the repository's docs/owner_principles.md),
      without the table rows whose evidence cites only this question or later ones;
    - three prompts: resolver, challenger (both with the instructions of the core agents), and a
      judge that compares the recommendation with the owner's real decision (only the judge sees it).
    Writes <out>/manifest.json. Selection: ``loop`` (default) = questions raised by loop agents.

    Then run, for each question in the manifest, three general-purpose subagents in order with the
    full content of resolve_prompt.md, challenge_prompt.md and judge_prompt.md (the /laop backtest
    mode of the skill does it).

report
    Reads the resolutions and judgements and writes <out>/report.md: what auto mode would have
    decided alone and whether it matched the owner, the quality of the recommendations, escalation
    of the questions where the owner had information of their own, and a simulation of the trust
    ledger in chronological order (<out>/ledger_seed.json).
"""

from __future__ import annotations

import io
import json
import re
import subprocess
import sys
import tarfile
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
AGENTS = SKILL / "core" / ".claude" / "agents"
DEFAULT_LOOP_AGENTS = ("builder",)
ENTRY = re.compile(r"(?m)^## (Q\d+):\s*(.*)$")
DECISION_START = re.compile(r"(?m)^\s*(?:-\s*)?\**Decision \(owner")
STATUS_LINE = re.compile(r"(?m)^(\s*-\s*Status:\s*)(.*)$")
TASK_ID = re.compile(r"\b([A-Z][A-Z0-9]*-\d+)\b")
QREF = re.compile(r"\bQ(\d+)\b")


# --------------------------------------------------------------------------- parsing


def entries(text: str) -> list[dict]:
    parts = ENTRY.split(text)
    out = []
    for i in range(1, len(parts), 3):
        qid, title, body = parts[i], parts[i + 1].strip(), parts[i + 2]
        raised = re.search(r"Raised by:?\s*([^\n]+)", body)
        out.append({"id": qid, "num": int(qid[1:]), "title": title, "body": body,
                    "raised_by": raised.group(1).strip().rstrip(".") if raised else "",
                    "decision": owner_decision(body)})
    return out


def owner_decision(body: str) -> str:
    m = DECISION_START.search(body)
    if m:
        return body[m.start():].strip()
    s = STATUS_LINE.search(body)
    if s and "decided" in s.group(2):
        return s.group(2).strip()
    return ""


def strip_decision(body: str) -> str:
    """The entry as it stood before the owner decided it."""
    m = DECISION_START.search(body)
    if m:
        body = body[:m.start()].rstrip() + "\n\n"
    return STATUS_LINE.sub(lambda s: s.group(1) + "open", body, count=1)


def loop_agents(repo: Path) -> tuple[str, ...]:
    """The dispatched roles of harness/roles.json (or the default builder)."""
    path = repo / "harness" / "roles.json"
    if not path.exists():
        return DEFAULT_LOOP_AGENTS
    roles = json.loads(path.read_text(encoding="utf-8")).get("roles", {})
    return tuple(name for name, r in roles.items() if r.get("dispatch")) or DEFAULT_LOOP_AGENTS


def is_loop_agent(raised_by: str, agents: tuple[str, ...] = DEFAULT_LOOP_AGENTS) -> bool:
    return any(a in raised_by.lower() for a in agents)


def filter_principles(text: str, qnum: int) -> str:
    """Drop the table rows whose evidence cites only question qnum or later ones."""
    out = []
    for line in text.splitlines():
        if line.startswith("|") and not line.startswith("|---"):
            refs = [int(n) for n in QREF.findall(line)]
            if refs and min(refs) >= qnum:
                continue
        out.append(line)
    return "\n".join(out) + "\n"


def agent_body(name: str) -> str:
    text = (AGENTS / f"{name}.md").read_text(encoding="utf-8")
    return text.split("---", 2)[2].strip() if text.startswith("---") else text


def git(repo: Path, *args: str, binary: bool = False):
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, check=True)
    return proc.stdout if binary else proc.stdout.decode("utf-8", "replace").strip()


def load_classification(path: Path | None) -> dict[str, dict]:
    """Lines 'Qn | raised_by | verdict | owner_info | note' (laop/analysis/oq_classification.md)."""
    out = {}
    if not path or not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in line.split("|")]
        if len(cells) >= 4 and re.fullmatch(r"Q\d+", cells[0]):
            out[cells[0]] = {"raised_by": cells[1], "verdict": cells[2], "owner_info": cells[3],
                             "note": cells[4] if len(cells) > 4 else ""}
    return out


# --------------------------------------------------------------------------- prepare


def prepare(repo: Path, out: Path, selection: str, principles: Path | None, classification: Path | None) -> int:
    project = json.loads((repo / "harness" / "project.json").read_text(encoding="utf-8")) \
        if (repo / "harness" / "project.json").exists() else {}
    oq_rel = project.get("open_questions", "docs/open_questions.md")
    principles = principles or repo / project.get("principles_file", "docs/owner_principles.md")
    if not principles.exists():
        print(f"No principles file at {principles}: pass principles=<file>.")
        return 1
    ptext = principles.read_text(encoding="utf-8")
    classes = load_classification(classification)
    all_entries = entries((repo / oq_rel).read_text(encoding="utf-8"))
    decided = [e for e in all_entries if e["decision"]]
    if selection == "all":
        chosen = decided
    elif selection == "loop":
        agents = loop_agents(repo)
        chosen = [e for e in decided if is_loop_agent(e["raised_by"], agents) or
                  classes.get(e["id"], {}).get("raised_by") == "loop-agent"]
    else:
        wanted = {q.strip() for q in selection.split(",")}
        chosen = [e for e in decided if e["id"] in wanted]
    out.mkdir(parents=True, exist_ok=True)
    resolver, challenger = agent_body("resolver"), agent_body("challenger")
    manifest = []
    for e in chosen:
        sha = git(repo, "log", "--reverse", "--format=%H", f"-S## {e['id']}:", "--", oq_rel).splitlines()
        if not sha:
            print(f"{e['id']}: commit not found, skipped")
            continue
        sha = sha[0]
        date = git(repo, "show", "-s", "--format=%ad", "--date=short", sha)
        qdir = out / e["id"]
        copy = qdir / "repo"
        if not copy.exists():
            data = git(repo, "archive", "--format=tar", sha, binary=True)
            with tarfile.open(fileobj=io.BytesIO(data)) as tar:
                tar.extractall(copy, filter="data")
        # the question as it stood before the decision, and nothing after it
        oq = copy / oq_rel
        text = oq.read_text(encoding="utf-8")
        parts = ENTRY.split(text)
        head, kept = parts[0], []
        entry_now = strip_decision(e["body"])  # fallback: today's entry without the decision
        for i in range(1, len(parts), 3):
            qid, title, body = parts[i], parts[i + 1], parts[i + 2]
            if int(qid[1:]) > e["num"]:
                break
            if qid == e["id"]:
                body = entry_now = strip_decision(body)
            kept.append(f"## {qid}: {title}{body}")
        oq.write_text(head + "".join(kept), encoding="utf-8")
        principles_copy = copy / "docs" / "owner_principles.md"
        principles_copy.parent.mkdir(parents=True, exist_ok=True)
        principles_copy.write_text(filter_principles(ptext, e["num"]), encoding="utf-8")
        task_text = ""
        tid = TASK_ID.search(e["raised_by"])
        tasks_file = copy / "harness" / "tasks.json"
        if tid and tasks_file.exists():
            try:
                for t in json.loads(tasks_file.read_text(encoding="utf-8")).get("tasks", []):
                    if t.get("id") == tid.group(1):
                        task_text = f"Task {t['id']}: {t.get('title', '')}\n{t.get('description', '')}"
            except ValueError:
                pass
        resolution = qdir / "resolution.md"
        judge = qdir / "judge.md"
        context = (
            "BACKTEST. You run on a copy of the repository as it was when the question was raised: "
            f"{copy}. Read only inside it (it is a plain copy, with no git history: skip git commands). "
            "Do not read any other directory, and do not try to find the owner's decision: it is hidden on "
            "purpose. Wherever your instructions mention the principles file, the open questions file or "
            f"the repository, they are {principles_copy}, {oq} and {copy}.\n\n"
            f"Question {e['id']} ({e['title']}), raised by {e['raised_by'] or 'unknown'} on {date}:\n\n"
            f"{entry_now.strip()}\n\n" + (f"Task context:\n{task_text}\n\n" if task_text else ""))
        (qdir / "resolve_prompt.md").write_text(
            f"You are the resolver. Your instructions:\n\n{resolver}\n\n---\n\n{context}"
            f"Write your resolution to {resolution} in the format of your instructions (first line "
            "'RESOLUTION: ANSWER' or 'RESOLUTION: ESCALATE', then 'Category:' with an R or D id of the "
            "principles file). Your final reply is only the first line.\n", encoding="utf-8")
        (qdir / "challenge_prompt.md").write_text(
            f"You are the challenger. Your instructions:\n\n{challenger}\n\n---\n\n{context}"
            f"The resolver's resolution is in {resolution}. Append your section at its end, starting with "
            "a line 'CHALLENGE: AGREE', 'CHALLENGE: AMEND', 'CHALLENGE: COUNTER' or 'CHALLENGE: ESCALATE'. "
            "Your final reply is only that line.\n", encoding="utf-8")
        (qdir / "judge_prompt.md").write_text(
            "You judge a backtest of an assistant that recommends decisions to a project owner. Compare "
            f"the recommendation in {resolution} (the resolver's Decision, as amended by the challenger's "
            "section if it amends or counters it) with the decision the owner actually took, below. Read "
            f"only that file and this prompt.\n\nThe owner's actual decision on {e['id']}:\n\n"
            f"{e['decision']}\n\nWrite {judge} with exactly these lines first:\n"
            "MATCH: same | partial | different\n"
            "OWNER_INFO: yes | no\n"
            "DEFECT: yes | no\n"
            "then one paragraph of reasons. same: the owner chose what was recommended (extra wording or "
            "minor precisions do not matter). partial: same core choice, but the owner added or changed "
            "something that changes what gets built. different: the owner chose another option or a "
            "substantially different answer. OWNER_INFO yes: the owner's decision relies on information "
            "or a preference that the repository did not contain (practice, external programs, scope, "
            "taste). DEFECT yes: where the recommendation differs from the owner's decision, the "
            "resolution shows a concrete case (an input, model or call, with its result) in which the "
            "owner's decision as written gives a wrong, misleading or silently plausible result, and "
            "nothing in the owner's decision addresses that case; name the case in the reasons. A "
            "difference is not a defect by itself. Your final reply is only the MATCH line.\n",
            encoding="utf-8")
        manifest.append({"qid": e["id"], "num": e["num"], "date": date, "sha": sha,
                         "raised_by": e["raised_by"], "dir": str(qdir), "repo": str(copy),
                         "resolve_prompt": str(qdir / "resolve_prompt.md"),
                         "challenge_prompt": str(qdir / "challenge_prompt.md"),
                         "judge_prompt": str(qdir / "judge_prompt.md"),
                         "resolution": str(resolution), "judge": str(judge),
                         "classification": classes.get(e["id"], {})})
    manifest.sort(key=lambda m: m["num"])
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"{len(manifest)} question(s) prepared in {out}: {', '.join(m['qid'] for m in manifest)}")
    return 0


# --------------------------------------------------------------------------- report


def first(pattern: str, text: str) -> str:
    m = re.search(pattern, text, re.MULTILINE | re.IGNORECASE)
    return m.group(1).upper() if m else ""


def report(out: Path, warmup: int = 3) -> int:
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    rows, ledger = [], {}
    for m in manifest:
        res = Path(m["resolution"]).read_text(encoding="utf-8") if Path(m["resolution"]).exists() else ""
        jud = Path(m["judge"]).read_text(encoding="utf-8") if Path(m["judge"]).exists() else ""
        resolver = first(r"^\s*RESOLUTION:\s*(ANSWER|ESCALATE)", res)
        category = first(r"^\s*Category:\s*([RD]\d+)", res)
        challenges = re.findall(r"(?m)^\s*CHALLENGE:\s*(AGREE|AMEND|COUNTER|ESCALATE)\s*$", res)
        challenger = challenges[-1] if challenges else ""
        match = first(r"^\s*MATCH:\s*(same|partial|different)", jud).lower()
        owner_info = first(r"^\s*OWNER_INFO:\s*(yes|no)", jud).lower()
        defect = first(r"^\s*DEFECT:\s*(yes|no)", jud).lower()
        agreed = resolver == "ANSWER" and challenger in ("AGREE", "AMEND")
        delegated = category.startswith("D")
        # trust ledger in chronological order (auto mode, warm-up = `warmup`)
        e = ledger.setdefault(category or "?", {"status": "warmup" if delegated else "owner", "streak": 0})
        auto_now = agreed and delegated and e["status"] == "auto"
        outcome = {"same": "agree", "partial": "amend", "different": "differ"}.get(match, "")
        if auto_now and outcome == "differ":
            e.update(status="warmup", streak=0)  # the owner would have overridden it
        elif outcome in ("agree", "amend"):
            e["streak"] += 1
            if e["status"] == "warmup" and e["streak"] >= warmup:
                e["status"] = "auto"
        elif outcome == "differ":
            e["streak"] = 0
        rows.append({**m, "resolver": resolver, "category": category, "challenger": challenger,
                     "agreed": agreed, "delegated": delegated, "auto_now": auto_now, "match": match,
                     "owner_info": owner_info, "defect": defect, "done": bool(res and jud)})
    done = [r for r in rows if r["done"]]
    n = len(done)

    def count(pred) -> int:
        return sum(1 for r in done if pred(r))

    would_alone = [r for r in done if r["agreed"] and r["delegated"]]
    lines = [
        f"# Resolver backtest ({n} of {len(rows)} questions completed)", "",
        "## What auto mode would have decided alone",
        "",
        ("Questions where the resolver answered, the challenger agreed or amended, and the category is "
        "delegated (D), i.e. decided alone once the category is autonomous:"),
        "",
        (f"- {len(would_alone)} of {n}: same as the owner {sum(r['match'] == 'same' for r in would_alone)}, "
        f"partial {sum(r['match'] == 'partial' for r in would_alone)}, "
        f"**different {sum(r['match'] == 'different' for r in would_alone)}** (wrong autonomous decisions)"),
        (f"- with the warm-up simulated in chronological order, actually decided alone: "
        f"{count(lambda r: r['auto_now'])} (wrong: {count(lambda r: r['auto_now'] and r['match'] == 'different')})"),
        "",
        "## Recommendations the owner would have read (advise mode)", "",
        (f"- same {count(lambda r: r['match'] == 'same')}, partial {count(lambda r: r['match'] == 'partial')}, "
        f"different {count(lambda r: r['match'] == 'different')}"),
        ("- a difference is not an error of the recommendation: where the judge found a concrete case "
        "the owner's decision gets wrong (DEFECT yes), check that decision in the project: "
        f"{', '.join(r['qid'] for r in done if r['defect'] == 'yes') or 'none'}"),
        "",
        "## Escalation", "",
        f"- escalated or not agreed: {count(lambda r: not (r['agreed'] and r['delegated']))} of {n}",
        (f"- questions where the owner had information of their own (judge OWNER_INFO yes): "
        f"{count(lambda r: r['owner_info'] == 'yes')}; of these sent to the owner: "
        f"{count(lambda r: r['owner_info'] == 'yes' and not (r['agreed'] and r['delegated']))}"),
        "",
        "## Trust ledger at the end (auto mode, warm-up 3)", "",
        *[f"- {cat}: {e['status']} ({e['streak']} accepted in a row)" for cat, e in sorted(ledger.items())],
        "",
        "## Per question", "",
        "| Q | date | raised by | resolver | category | challenger | alone? | owner | judge | owner info | defect | class |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
        *[f"| {r['qid']} | {r['date']} | {r['raised_by'][:30]} | {r['resolver'] or '-'} | {r['category'] or '-'} | "
          f"{r['challenger'] or '-'} | {'auto' if r['auto_now'] else ('would' if r['agreed'] and r['delegated'] else 'owner')} | "
          f"{'done' if r['done'] else 'pending'} | {r['match'] or '-'} | {r['owner_info'] or '-'} | {r['defect'] or '-'} | "
          f"{r['classification'].get('verdict', '-')}/{r['classification'].get('owner_info', '-')} |"
          for r in rows],
        "",
        ("Leakage notes: the principles file keeps rows with no question reference (e.g. owner principles "
        "stated later, gate decisions); the agents were told not to read outside the copy, which is not "
        "enforced."),
        "",
    ]
    (out / "report.md").write_text("\n".join(lines), encoding="utf-8")
    seed = {cat: {**e, "obvious": 0, "history": []} for cat, e in ledger.items() if cat != "?"}
    (out / "ledger_seed.json").write_text(json.dumps(seed, indent=2), encoding="utf-8")
    print("\n".join(lines[:16]))
    print(f"\nFull report: {out / 'report.md'}")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[0] not in ("prepare", "report"):
        print(__doc__)
        return 2
    opts = dict(a.split("=", 1) for a in argv[2:] if "=" in a)
    if argv[0] == "report":
        return report(Path(argv[1]).resolve(), int(opts.get("warmup", 3)))
    if len(argv) < 3:
        print("prepare needs <repo> <out>")
        return 2
    return prepare(Path(argv[1]).resolve(), Path(argv[2]).resolve(), opts.get("questions", "loop"),
                   Path(opts["principles"]) if "principles" in opts else None,
                   Path(opts["classification"]) if "classification" in opts else None)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main(sys.argv[1:]))
