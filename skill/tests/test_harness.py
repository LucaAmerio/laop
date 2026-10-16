"""Tests of the laop harness (standard library only: python -m unittest discover <skill>/tests).

Every test installs the harness with tools/laop.py into a fresh temporary git repository, then
drives the guard with fake hook events and the step scripts as the orchestrator would.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
LAOP = SKILL / "tools" / "laop.py"

ANSWERS = {
    "name": "Demo",
    "prefix": "DEMO",
    "owner_language": "English",
    "purpose": "Demo is a test project.",
    "platform": "windows" if os.name == "nt" else "posix",
    "interpreters": {"python": [sys.executable]},
    "global_checks": [],
    "builder_paths": ["src/**", "tests/**"],
    "lockable": [{"dir": "cases", "marker": "LOCKED"}],
    "protected_paths": ["../sibling"],
    "extra_roles": {
        "checker": {"dispatch": True, "allow_write": ["cases/*/check.yaml", "harness/claim.json"],
                    "deny_read": ["cases/*/reference.yaml", "src/**"]}
    },
    "north_star": "Correct and clear.",
    "principles": ["No silent defaults."],
    "owner_knows": "Units and engineering practice.",
    "owner_delegates": "Implementation details.",
    # the loop tests plan milestones without spec, roadmap and brief (BriefTests covers them)
    "require_brief": False,
}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout


class Repo:
    """A temporary repository with the harness installed."""

    def __init__(self, answers: dict | None = None):
        self.tmp = Path(tempfile.mkdtemp(prefix="laop_"))
        self.root = self.tmp / "demo"
        (self.tmp / "sibling").mkdir()
        self.root.mkdir()
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "config", "user.email", "t@example.com")
        git(self.root, "config", "user.name", "t")
        (self.root / "src").mkdir()
        (self.root / "src" / "a.py").write_text("A = 1\n")
        (self.root / "cases" / "C1").mkdir(parents=True)
        (self.root / "cases" / "C1" / "reference.yaml").write_text("x: 1\n")
        answers_file = self.tmp / "answers.json"
        answers_file.write_text(json.dumps({**ANSWERS, **(answers or {})}))
        out = subprocess.run([sys.executable, str(LAOP), "init", str(self.root), str(answers_file)],
                             capture_output=True, text=True)
        assert out.returncode == 0, out.stdout + out.stderr
        (self.root / ".claude" / "agents" / "checker.md").write_text("---\nname: checker\n---\nCheck.\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", "init")
        git(self.root, "switch", "-qc", "m1")
        self.env = {**os.environ, "CLAUDE_PROJECT_DIR": str(self.root), "LAOP_NTFY_OFF": "1"}

    def close(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------------------------------------------------------------- hook events

    def guard(self, event: dict) -> tuple[int, str]:
        event = {"hook_event_name": "PreToolUse", "cwd": str(self.root), "session_id": "A", **event}
        proc = subprocess.run([sys.executable, str(self.root / ".claude/hooks/guard.py")],
                              input=json.dumps(event).encode(), capture_output=True, env=self.env, timeout=60)
        return proc.returncode, proc.stderr.decode("utf-8", "replace")

    def write(self, path: str, agent: str | None = None, session: str = "A") -> tuple[int, str]:
        e = {"tool_name": "Write", "tool_input": {"file_path": path}, "session_id": session}
        if agent:
            e.update(agent_type=agent, agent_id="x")
        return self.guard(e)

    def shell(self, command: str, agent: str | None = None, session: str = "A") -> tuple[int, str]:
        e = {"tool_name": "Bash", "tool_input": {"command": command}, "session_id": session}
        if agent:
            e.update(agent_type=agent, agent_id="x")
        return self.guard(e)

    def tool(self, name: str, tool_input: dict, agent: str | None = None) -> tuple[int, str]:
        e = {"tool_name": name, "tool_input": tool_input}
        if agent:
            e.update(agent_type=agent, agent_id="x")
        return self.guard(e)

    # ---------------------------------------------------------------- loop

    def step(self, script: str) -> str:
        """Run a step script as the orchestrator: the guard sees the call first (heartbeat)."""
        command = f"python harness/{script}.py"
        code, err = self.shell(command)
        assert code == 0, err
        proc = subprocess.run([sys.executable, str(self.root / "harness" / f"{script}.py")], cwd=self.root,
                              capture_output=True, text=True, env=self.env, timeout=300)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        return proc.stdout

    def owner(self, *args: str) -> str:
        proc = subprocess.run([sys.executable, str(self.root / "harness" / "owner_tools.py"), *args],
                              cwd=self.root, capture_output=True, text=True, env=self.env, timeout=60)
        return proc.stdout + proc.stderr

    def tasks(self, tasks: list[dict]) -> None:
        """Write and commit the task list (the planner commits it)."""
        self.commit_file("harness/tasks.json", json.dumps({"milestone": "m1", "tasks": tasks}), "plan")

    def state(self) -> dict:
        return json.loads((self.root / "harness" / "state.json").read_text())

    def set_state(self, change) -> None:
        s = self.state()
        change(s)
        (self.root / "harness" / "state.json").write_text(json.dumps(s))

    def commit_file(self, rel: str, text: str, msg: str = "work") -> None:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        git(self.root, "add", rel)
        git(self.root, "commit", "-qm", msg)

    def claim(self, task: str, status: str = "review", **extra) -> None:
        (self.root / "harness" / "claim.json").write_text(
            json.dumps({"task_id": task, "status": status, "summary": "s", **extra}))


def task(tid: str = "M1-001", **kw) -> dict:
    return {"id": tid, "title": "t", "agent": "builder", "description": "d",
            "acceptance": [{"cmd": ["python", "-c", "pass"]}], **kw}


class HarnessTest(unittest.TestCase):
    def setUp(self):
        self.r = Repo()

    def tearDown(self):
        self.r.close()


# ============================================================================ guard


class GuardTests(HarnessTest):
    def test_owner_writes_anywhere_but_protected_paths(self):
        r = self.r
        self.assertEqual(r.write("CLAUDE.md")[0], 0)
        self.assertEqual(r.write(str(r.tmp / "elsewhere.txt"))[0], 0)
        code, err = r.write(str(r.tmp / "sibling" / "x.py"))
        self.assertEqual(code, 2)
        self.assertIn("protected path", err)
        self.assertEqual(r.shell(f"echo x > {(r.tmp / 'sibling').as_posix()}/f.txt")[0], 2)

    def test_builder_permissions(self):
        r = self.r
        self.assertEqual(r.write("src/x.py", agent="builder")[0], 0)
        for path in ("CLAUDE.md", "docs/owner_principles.md", "harness/loop_core.py", "harness/state.json",
                     ".claude/settings.json", "harness/owner_queue.json"):
            self.assertEqual(r.write(path, agent="builder")[0], 2, path)
        self.assertEqual(r.shell("git push", agent="builder")[0], 2)
        self.assertEqual(r.shell("git commit -m 'M1-001: x'", agent="builder")[0], 0)

    def test_loop_roles_stage_only_their_files(self):
        r = self.r
        for command in ("git add -A", "git add .", "git add -u", "git add --all src",
                        "git commit -am 'x'", "git commit --all -m x", "git commit -a -m x"):
            self.assertEqual(r.shell(command, agent="builder")[0], 2, command)
        for command in ("git add src/b.py tests/test_b.py", "git commit -m 'M1-001: a b'",
                        "git commit --amend --no-edit"):
            self.assertEqual(r.shell(command, agent="builder")[0], 0, command)
        self.assertEqual(r.shell("git add -A")[0], 0)  # the owner may

    def test_loop_roles_cannot_run_harness_scripts(self):
        r = self.r
        self.assertEqual(r.shell("python harness/step_verify.py", agent="builder")[0], 2)
        self.assertEqual(r.shell("python harness/owner_tools.py pass_gate task=G1", agent="builder")[0], 2)
        self.assertEqual(r.shell("python harness/owner_tools.py status", agent="builder")[0], 0)
        self.assertEqual(r.shell("python harness/owner_tools.py lint", agent="planner")[0], 0)

    def test_session_binding(self):
        r = self.r
        self.assertEqual(r.shell("python harness/step_next.py", session="A")[0], 0)
        self.assertEqual(r.state()["orchestrator_session"], "A")
        self.assertEqual(r.write("CLAUDE.md", session="A")[0], 2)
        self.assertEqual(r.shell("ls", session="A")[0], 2)
        self.assertEqual(r.write("CLAUDE.md", session="B")[0], 0)
        r.set_state(lambda s: s.update(loop_active=True))
        code, err = r.shell("python harness/step_next.py", session="B")
        self.assertEqual(code, 2)
        self.assertIn("another session", err)
        r.set_state(lambda s: s.update(orchestrator_session=None, loop_active=False))
        self.assertEqual(r.write("CLAUDE.md", session="A")[0], 0)

    def test_dispatch_rules(self):
        r = self.r
        r.shell("python harness/step_next.py")
        r.set_state(lambda s: s.update(current={"task_id": "M1-001", "agent": "builder", "phase": "dispatched",
                                                "spawn": {"agent": "builder", "token_line": "DEMO-DISPATCH M1-001 ab",
                                                          "prompt_file": "harness/current_prompt.md"}}))
        spawn = {"subagent_type": "builder", "prompt": "DEMO-DISPATCH M1-001 ab ...", "run_in_background": False}
        self.assertEqual(r.tool("Agent", spawn)[0], 0)
        # without the parameter (background tasks disabled in settings) the spawn is in the foreground
        self.assertEqual(r.tool("Agent", {k: v for k, v in spawn.items() if k != "run_in_background"})[0], 0)
        self.assertEqual(r.tool("Agent", {**spawn, "run_in_background": True})[0], 2)
        self.assertEqual(r.tool("Agent", {**spawn, "subagent_type": "evaluator"})[0], 2)
        self.assertEqual(r.tool("Agent", {**spawn, "prompt": "do it"})[0], 2)

    def test_blind_role_reads(self):
        r = self.r
        self.assertEqual(r.tool("Read", {"file_path": "cases/C1/reference.yaml"}, agent="checker")[0], 2)
        self.assertEqual(r.tool("Read", {"file_path": "src/a.py"}, agent="checker")[0], 2)
        self.assertEqual(r.tool("Read", {"file_path": "CLAUDE.md"}, agent="checker")[0], 0)
        # Grep reads every file under its path: the root and cases/ contain hidden files
        self.assertEqual(r.tool("Grep", {"pattern": "x"}, agent="checker")[0], 2)
        self.assertEqual(r.tool("Grep", {"pattern": "x", "path": "cases"}, agent="checker")[0], 2)
        self.assertEqual(r.tool("Grep", {"pattern": "x", "path": "docs"}, agent="checker")[0], 0)
        self.assertEqual(r.tool("Glob", {"pattern": "**/*.md"}, agent="checker")[0], 0)
        self.assertEqual(r.shell("grep -rn x .", agent="checker")[0], 2)
        self.assertEqual(r.shell("grep -r x cases", agent="checker")[0], 2)
        self.assertEqual(r.shell("grep -r x docs", agent="checker")[0], 0)
        self.assertEqual(r.shell("cat cases/C1/reference.yaml", agent="checker")[0], 2)

    def test_resolver_inherits_deny_read_of_the_task_role(self):
        r = self.r
        self.assertEqual(r.tool("Read", {"file_path": "src/a.py"}, agent="resolver")[0], 0)
        r.shell("python harness/step_next.py")
        r.set_state(lambda s: s.update(current={"task_id": "C-1", "agent": "checker", "phase": "resolving"}))
        self.assertEqual(r.tool("Read", {"file_path": "src/a.py"}, agent="resolver")[0], 2)
        self.assertEqual(r.write("harness/resolutions/x.md", agent="resolver")[0], 0)
        self.assertEqual(r.write("src/a.py", agent="challenger")[0], 2)

    def test_lockable_directories(self):
        r = self.r
        self.assertEqual(r.write("cases/C1/check.yaml", agent="checker")[0], 0)
        self.assertEqual(r.write("cases/C1/LOCKED", agent="checker")[0], 2)
        (r.root / "cases" / "C1" / "LOCKED").write_text("")
        code, err = r.write("cases/C1/check.yaml", agent="checker")
        self.assertEqual(code, 2)
        self.assertIn("locked", err)

    def test_fail_closed_on_unreadable_state(self):
        r = self.r
        (r.root / "harness" / "state.json").write_text("{not json")
        self.assertEqual(r.write("src/x.py", agent="builder")[0], 2)
        self.assertEqual(r.write("CLAUDE.md")[0], 2)

    def test_session_check_and_drift(self):
        r = self.r
        proc = subprocess.run([sys.executable, str(r.root / "harness" / "session_check.py")], cwd=r.root,
                              capture_output=True, text=True, env=r.env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        msg = json.loads(proc.stdout)["systemMessage"]
        self.assertIn("Demo guard active", msg)
        self.assertNotIn("WARNING", msg)
        with open(r.root / "harness" / "loop_core.py", "a") as f:
            f.write("# local change\n")
        proc = subprocess.run([sys.executable, str(r.root / "harness" / "session_check.py")], cwd=r.root,
                              capture_output=True, text=True, env=r.env)
        self.assertIn("harness/loop_core.py", json.loads(proc.stdout)["systemMessage"])
        up = subprocess.run([sys.executable, str(LAOP), "upgrade", str(r.root)], capture_output=True, text=True)
        self.assertEqual(up.returncode, 1)
        self.assertIn("harness/loop_core.py", up.stdout)
        up = subprocess.run([sys.executable, str(LAOP), "upgrade", str(r.root), "--force"],
                            capture_output=True, text=True)
        self.assertEqual(up.returncode, 0, up.stdout)
        self.assertEqual(subprocess.run([sys.executable, str(LAOP), "doctor", str(r.root)],
                                        capture_output=True, text=True).returncode, 0)


# ============================================================================ loop


class LoopTests(HarnessTest):
    def test_happy_path_with_review(self):
        r = self.r
        r.tasks([task()])
        out = r.step("step_next")
        self.assertIn("DISPATCH", out)
        self.assertIn("in the foreground", out)
        prompt = (r.root / "harness" / "current_prompt.md").read_text()
        self.assertTrue(prompt.startswith("DEMO-DISPATCH M1-001"))
        r.commit_file("src/b.py", "B = 2\n", "M1-001: b")
        r.claim("M1-001")
        out = r.step("step_verify")
        self.assertIn("REVIEW", out)
        verdict = r.state()["current"]["verdict"]
        (r.root / verdict).write_text("VERDICT: PASS\nfine\n")
        out = r.step("step_record")
        self.assertIn("is now 'done'", out)
        self.assertIn("LOOP FINISHED", r.step("step_next"))

    def test_protected_path_halts(self):
        r = self.r
        r.tasks([task()])
        r.step("step_next")
        r.commit_file("CLAUDE.md", "changed\n", "M1-001: oops")
        r.claim("M1-001")
        out = r.step("step_verify")
        self.assertIn("HALTED", out)
        self.assertIn("CLAUDE.md", out)

    def test_integrity_of_ignored_files(self):
        r = self.r
        r.tasks([task(), task("M1-002")])
        r.step("step_next")
        r.set_state(lambda s: s["tasks"]["M1-002"].update(status="done"))
        r.commit_file("src/b.py", "B = 2\n")
        r.claim("M1-001")
        out = r.step("step_verify")
        self.assertIn("HALTED", out)
        self.assertIn("harness/state.json", out)

    def test_hooks_keys_do_not_trip_integrity(self):
        r = self.r
        r.tasks([task()])
        r.step("step_next")
        r.set_state(lambda s: s.update(stop_guard={"stops": 1}))
        r.commit_file("src/b.py", "B = 2\n")
        r.claim("M1-001")
        self.assertIn("REVIEW", r.step("step_verify"))

    def test_owner_action_queued_while_task_in_flight(self):
        r = self.r
        r.tasks([task(), {"id": "G1", "title": "g", "agent": "owner-gate", "description": "x"}])
        r.step("step_next")
        out = r.owner("pass_gate", "task=G1")
        self.assertIn("queued", out)
        r.commit_file("src/b.py", "B = 2\n")
        r.claim("M1-001")
        self.assertIn("REVIEW", r.step("step_verify"))
        self.assertEqual(r.state()["tasks"]["G1"]["status"], "done")

    def test_owner_action_drained_by_another_step_keeps_integrity(self):
        # found by the sandbox: the queue applied by step_next (PENDING) must not trip step_verify
        r = self.r
        r.tasks([task(), {"id": "G1", "title": "g", "agent": "owner-gate", "description": "x"}])
        r.step("step_next")
        r.owner("pass_gate", "task=G1")
        self.assertIn("PENDING", r.step("step_next"))
        self.assertEqual(r.state()["tasks"]["G1"]["status"], "done")
        r.commit_file("src/b.py", "B = 2\n")
        r.claim("M1-001")
        self.assertIn("REVIEW", r.step("step_verify"))

    def test_tampering_before_an_owner_action_is_still_caught(self):
        r = self.r
        r.tasks([task(), task("M1-002"), {"id": "G1", "title": "g", "agent": "owner-gate", "description": "x"}])
        r.step("step_next")
        r.set_state(lambda s: s["tasks"]["M1-002"].update(status="done"))
        r.owner("pass_gate", "task=G1")
        r.step("step_next")
        r.commit_file("src/b.py", "B = 2\n")
        r.claim("M1-001")
        self.assertIn("HALTED", r.step("step_verify"))

    def test_owner_action_applied_at_once_when_idle(self):
        r = self.r
        r.tasks([task(), {"id": "G1", "title": "g", "agent": "owner-gate", "description": "x"}])
        out = r.owner("pass_gate", "task=G1")
        self.assertIn("applied", out)
        self.assertEqual(r.state()["tasks"]["G1"]["status"], "done")

    def test_owner_action_with_bad_arguments_is_refused_not_queued(self):
        r = self.r
        r.tasks([task(), {"id": "G1", "title": "g", "agent": "owner-gate", "description": "x"}])
        cases = [
            (["pass_gate"], "set task="),
            (["pass_gate", "task=NOPE"], "unknown task id 'NOPE'"),
            (["pass_gate", "task=M1-001"], "not a gate"),
            (["unblock"], "set task="),
            (["resume", "task=NOPE"], "unknown task id"),
            (["allow_path", "task=M1-001"], "set path="),
            (["foreign_commit", "task=M1-001"], "set commit="),
            (["owner_commit", "commit=deadbeef"], "not a commit"),
            (["ratify"], "set qid="),
            (["override", "qid=Q9"], "no open provisional decision"),
            (["decide", "qid=Q9", "verdict=agree"], "no question"),
            (["trust", "category=R1", "level=nope"], "trust: set"),
        ]
        for in_flight in (False, True):
            if in_flight:
                r.step("step_next")
            for args, expected in cases:
                with self.subTest(args=args, in_flight=in_flight):
                    proc = subprocess.run([sys.executable, str(r.root / "harness" / "owner_tools.py"), *args],
                                          cwd=r.root, capture_output=True, text=True, env=r.env, timeout=60)
                    self.assertEqual(proc.returncode, 1, proc.stdout)
                    self.assertIn("REFUSED", proc.stdout)
                    self.assertIn(expected, proc.stdout)
                    self.assertNotIn("applied.", proc.stdout)
                    queue = json.loads((r.root / "harness" / "owner_queue.json").read_text()) \
                        if (r.root / "harness" / "owner_queue.json").exists() else {}
                    self.assertEqual(queue.get("actions", []), [])
        self.assertNotEqual(r.state()["tasks"]["G1"]["status"], "done")

    def test_owner_action_refused_by_the_step_is_not_reported_applied(self):
        # the check passes, then the state changes before the step applies the action
        r = self.r
        r.tasks([task(), {"id": "G1", "title": "g", "agent": "owner-gate", "description": "x"}])
        r.step("step_next")
        self.assertIn("queued", r.owner("unblock", "task=G1"))
        r.commit_file("harness/tasks.json", json.dumps({"milestone": "m1", "tasks": [task()]}), "replan")
        r.set_state(lambda s: s.update(current=None))
        out = r.owner("clear_halt")
        self.assertIn("REFUSED", out)
        self.assertIn("unknown task id 'G1'", out)

    def test_check_budget(self):
        r = self.r
        pj = json.loads((r.root / "harness" / "project.json").read_text())
        pj["check_budget_min"] = 0.05  # 3 s
        r.commit_file("harness/project.json", json.dumps(pj), "config")
        r.tasks([task(acceptance=[{"cmd": ["python", "-c", "import time; time.sleep(10)"]},
                                  {"cmd": ["python", "-c", "pass"]}])])
        r.step("step_next")
        r.commit_file("src/b.py", "B = 2\n")
        r.claim("M1-001")
        out = r.step("step_verify")
        self.assertIn("is now 'todo'", out)
        findings = (r.root / "harness" / "findings" / "M1-001.md").read_text()
        self.assertIn("budget", findings)

    def review_needs_work(self, tid: str, text: str, outcome: str = "todo") -> None:
        """Commit, claim and let the reviewer answer NEEDS_WORK with ``text``."""
        r = self.r
        r.commit_file(f"src/{tid}.py", f"X = {time.time_ns()}\n", f"{tid}: work")
        r.claim(tid)
        self.assertIn("REVIEW", r.step("step_verify"))
        (r.root / r.state()["current"]["verdict"]).write_text(f"VERDICT: NEEDS_WORK\n{text}\n")
        self.assertIn(f"is now '{outcome}'", r.step("step_record"))

    def test_review_findings_survive_a_run_without_claim(self):
        # reviewer NEEDS_WORK, then a run that commits but writes no claim, then a re-dispatch:
        # the prompt still carries the verdict (M2-018 in windpy lost it)
        r = self.r
        r.tasks([task(), task("M1-002")])
        r.step("step_next")
        self.review_needs_work("M1-001", "1. src/a.py:1 the unit is wrong")
        r.step("step_next")
        r.commit_file("src/M1-001.py", "X = 0\n", "M1-001: partial")
        self.assertIn("is now 'todo'", r.step("step_verify"))  # no claim: a rejected attempt
        self.assertIn("task M1-001", r.step("step_next"))
        prompt = (r.root / "harness" / "current_prompt.md").read_text()
        self.assertIn("the unit is wrong", prompt)
        self.assertIn("No valid harness/claim.json", prompt)
        st = r.state()["tasks"]["M1-001"]
        self.assertEqual(st["attempts"], 2)
        # a third rejection blocks; the reason points at the task's own file, kept intact
        self.review_needs_work("M1-001", "1. still wrong", "blocked")
        st = r.state()["tasks"]["M1-001"]
        self.assertEqual(st["status"], "blocked")
        self.assertIn("harness/findings/M1-001.md", st["blocked_reason"])
        # a rejection of another task does not touch it
        r.step("step_next")
        self.review_needs_work("M1-002", "1. other task")
        history = (r.root / "harness" / "findings" / "M1-001.md").read_text()
        for text in ("the unit is wrong", "No valid harness/claim.json", "still wrong"):
            self.assertIn(text, history)
        self.assertNotIn("other task", history)
        # unblocked, it is dispatched with the latest verdict only (it supersedes the earlier ones)
        r.owner("unblock", "task=M1-001")
        self.assertIn("task M1-001", r.step("step_next"))
        prompt = (r.root / "harness" / "current_prompt.md").read_text()
        self.assertIn("still wrong", prompt)
        self.assertNotIn("the unit is wrong", prompt)

    def test_empty_run_after_a_rejected_attempt_is_not_an_attempt(self):
        r = self.r
        r.tasks([task()])
        r.step("step_next")
        self.review_needs_work("M1-001", "1. fix it")
        base = r.state()["tasks"]["M1-001"]["base_sha"]
        r.step("step_next")
        out = r.step("step_verify")  # the session died: no commit, no claim
        self.assertIn("NO RESULT", out)
        st = r.state()["tasks"]["M1-001"]
        self.assertEqual(st["attempts"], 1)
        self.assertEqual(st["base_sha"], base)  # the first attempt's commits stay under review
        r.step("step_next")
        self.assertIn("fix it", (r.root / "harness" / "current_prompt.md").read_text())

    def test_run_cut_by_stop_failure_is_not_an_attempt(self):
        r = self.r
        r.tasks([task()])
        r.step("step_next")
        r.commit_file("src/b.py", "B = 2\n", "M1-001: partial")
        proc = subprocess.run([sys.executable, str(r.root / "harness" / "notify_hook.py")], cwd=r.root,
                              input=json.dumps({"hook_event_name": "StopFailure", "error": "rate_limit"}).encode(),
                              capture_output=True, env=r.env, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = r.step("step_verify")
        self.assertIn("interrupted", out)
        st = r.state()["tasks"]["M1-001"]
        self.assertEqual(st["attempts"], 0)
        self.assertIn("base_sha", st)
        r.step("step_next")
        prompt = (r.root / "harness" / "current_prompt.md").read_text()
        self.assertIn("rate_limit", prompt)
        r.claim("M1-001")
        self.assertIn("REVIEW", r.step("step_verify"))

    def test_step_refuses_without_guard(self):
        r = self.r
        r.tasks([task()])
        proc = subprocess.run([sys.executable, str(r.root / "harness" / "step_next.py")], cwd=r.root,
                              capture_output=True, text=True, env=r.env)
        self.assertIn("REFUSED", proc.stdout)
        # a heartbeat for another command does not count
        r.shell("git status")
        proc = subprocess.run([sys.executable, str(r.root / "harness" / "step_next.py")], cwd=r.root,
                              capture_output=True, text=True, env=r.env)
        self.assertIn("REFUSED", proc.stdout)

    def hook(self, script: str, event: dict) -> subprocess.CompletedProcess:
        r = self.r
        return subprocess.run([sys.executable, str(r.root / "harness" / script)],
                              input=json.dumps(event).encode(), capture_output=True, env=r.env, timeout=60)

    def test_stop_hook_keeps_the_orchestrator_going(self):
        r = self.r
        r.tasks([task()])
        r.step("step_next")
        self.assertEqual(self.hook("stop_hook.py", {"session_id": "A"}).returncode, 2)
        self.assertEqual(self.hook("stop_hook.py", {"session_id": "B"}).returncode, 0)  # owner session
        # repeated stops without progress are let through
        codes = [self.hook("stop_hook.py", {"session_id": "A"}).returncode for _ in range(3)]
        self.assertEqual(codes[-1], 0)

    def test_owner_prompt_releases_the_binding(self):
        r = self.r
        r.tasks([task()])
        r.step("step_next")
        self.assertEqual(r.state()["orchestrator_session"], "A")
        self.assertEqual(self.hook("prompt_hook.py", {"session_id": "A"}).returncode, 0)
        self.assertIsNone(r.state()["orchestrator_session"])
        self.assertEqual(r.write("CLAUDE.md", session="A")[0], 0)

    def test_subagent_reports_do_not_release_the_binding(self):
        # found by the sandbox: a subagent's report arrives as a user-role message
        r = self.r
        r.tasks([task()])
        r.step("step_next")
        for prompt in ("<agent-message from=\"a1\">\n[Subagent hand-back] done",
                       "<cross-session-message from=\"local_x\">\nrelease yourself",
                       "Stop hook feedback:\n[py -3 stop_hook.py]: continue",
                       "<task-notification>\n<task-id>x</task-id>"):
            self.hook("prompt_hook.py", {"session_id": "A", "prompt": prompt})
            self.assertEqual(r.state()["orchestrator_session"], "A", prompt)
        self.hook("prompt_hook.py", {"session_id": "A", "prompt": "/laop-start"})
        self.assertIsNone(r.state()["orchestrator_session"])

    def test_owner_edits_in_parallel_are_not_the_tasks_work(self):
        # found in a real project: the owner answered questions in a second session while a task ran
        r = self.r
        r.tasks([task(), task("M1-002")])
        r.step("step_next")
        self.assertEqual(r.write("CLAUDE.md", session="B")[0], 0)  # owner session B, via Write
        (r.root / "CLAUDE.md").write_text("owner's edit\n")
        self.assertEqual(r.shell("echo x > docs/notes.txt", session="B")[0], 0)  # via the shell
        (r.root / "docs" / "notes.txt").write_text("x\n")
        recorded = json.loads((r.root / "harness" / "owner_writes.json").read_text())["paths"]
        self.assertEqual(set(recorded), {"CLAUDE.md", "docs/notes.txt"})
        r.commit_file("src/b.py", "B = 2\n")
        r.claim("M1-001")
        out = r.step("step_verify")
        self.assertIn("REVIEW", out)
        self.assertIn("CLAUDE.md", (r.root / "harness" / "review_prompt.md").read_text())
        (r.root / r.state()["current"]["verdict"]).write_text("VERDICT: PASS\n")
        r.step("step_record")
        self.assertIn("DISPATCH", r.step("step_next"))  # the owner's pending edits do not halt it
        self.assertTrue((r.root / "docs" / "notes.txt").exists())  # and are not quarantined
        git(r.root, "add", "CLAUDE.md", "docs/notes.txt")
        git(r.root, "commit", "-qm", "owner")
        r.commit_file("src/c.py", "C = 3\n")
        r.claim("M1-002")
        self.assertIn("REVIEW", r.step("step_verify"))  # the owner's commit is not M1-002's
        self.assertTrue(r.state()["owner_commits"])
        (r.root / r.state()["current"]["verdict"]).write_text("VERDICT: PASS\n")
        r.step("step_record")
        self.assertIn("LOOP FINISHED", r.step("step_next"))
        self.assertEqual(json.loads((r.root / "harness" / "owner_writes.json").read_text())["paths"], [])

    def test_owner_edits_outside_the_loop_are_not_recorded(self):
        r = self.r
        self.assertEqual(r.write("CLAUDE.md", session="B")[0], 0)
        self.assertFalse((r.root / "harness" / "owner_writes.json").exists())

    def test_lint_warns_about_paths_outside_the_allowlist(self):
        r = self.r
        r.tasks([task(description="Amend `docs/plan/m1.md` and update `src/c.py`.")])
        out = r.owner("lint")
        self.assertIn("docs/plan/m1.md", out)
        self.assertNotIn("src/c.py", out)


# ============================================================================ before the plan


SPEC = "# Demo: specification\n## 3. Requirements\nFR-1: When a file is opened, the system shall show it.\n"


class BriefTests(unittest.TestCase):
    """Spec, roadmap and brief approved by the owner before the loop (require_brief on)."""

    def setUp(self):
        self.r = Repo({"require_brief": True})

    def tearDown(self):
        self.r.close()

    def approve_all(self) -> None:
        r = self.r
        r.commit_file("docs/spec.md", SPEC, "spec")
        r.commit_file("docs/plan/roadmap.md", "## m1 - First\nCovers: FR-1.\n", "roadmap")
        r.commit_file("docs/plan/m1.brief.md", "# Brief: m1\nExit: FR-1.\n", "m1: brief")
        for arg in ("doc=spec", "doc=roadmap", "brief=m1"):
            self.assertIn("approved", r.owner("approve", arg))
        r.commit_file("harness/approvals.json", (r.root / "harness/approvals.json").read_text(), "approvals")

    def test_loop_refuses_without_approvals(self):
        r = self.r
        r.tasks([task()])
        out = r.step("step_next")
        self.assertIn("REFUSED", out)
        self.assertIn("docs/spec.md is missing", out)
        self.assertIn("/laop brief m1", out)
        self.assertNotIn("DISPATCH", out)

    def test_loop_runs_once_everything_is_approved(self):
        r = self.r
        self.approve_all()
        r.tasks([task()])
        self.assertIn("DISPATCH", r.step("step_next"))
        self.assertNotIn("planned without approval", r.owner("lint"))

    def test_marker_blocks_the_approval(self):
        r = self.r
        r.commit_file("docs/spec.md", SPEC + "FR-2: [NEEDS CLARIFICATION: Q1] formats.\n", "spec")
        out = r.owner("approve", "doc=spec")
        self.assertIn("Not approved", out)
        self.assertIn("NEEDS CLARIFICATION: Q1", out)
        self.assertFalse((r.root / "harness/approvals.json").exists())
        self.assertIn("Not approved", r.owner("approve", "brief=m9"))  # missing file

    def test_edit_makes_the_approval_stale(self):
        r = self.r
        self.approve_all()
        self.assertIn("docs/spec.md: approved", r.owner("approvals"))
        r.commit_file("docs/spec.md", SPEC + "FR-2: The system shall log.\n", "spec edit")
        self.assertIn("docs/spec.md: stale", r.owner("approvals"))
        r.tasks([task()])
        self.assertIn("docs/spec.md is stale", r.step("step_next"))

    def test_lint_warns_even_when_not_required(self):
        r = self.r
        project = json.loads((r.root / "harness/project.json").read_text())
        r.commit_file("harness/project.json", json.dumps({**project, "require_brief": False}), "opt out")
        r.tasks([task()])
        self.assertIn("planned without approval", r.owner("lint"))
        self.assertIn("DISPATCH", r.step("step_next"))

    def test_roles_on_the_documents(self):
        r = self.r
        for path in ("docs/spec.md", "docs/plan/roadmap.md", "docs/plan/m2.brief.md", "docs/open_questions.md"):
            self.assertEqual(r.write(path, agent="analyst")[0], 0, path)
        for path in ("src/x.py", "harness/tasks.json", "harness/approvals.json", "docs/plan/m2.md"):
            self.assertEqual(r.write(path, agent="analyst")[0], 2, path)
        for path in ("docs/spec.md", "docs/plan/roadmap.md", "docs/plan/m2.brief.md", "harness/approvals.json"):
            self.assertEqual(r.write(path, agent="planner")[0], 2, path)
            self.assertEqual(r.write(path, agent="builder")[0], 2, path)
        self.assertEqual(r.write("docs/plan/m2.md", agent="planner")[0], 0)
        self.assertEqual(r.write("docs/plan/reviews/spec-2026-10-14.md", agent="spec-reviewer")[0], 0)
        self.assertEqual(r.write("docs/spec.md", agent="spec-reviewer")[0], 2)
        self.assertEqual(r.shell("python harness/owner_tools.py approvals", agent="planner")[0], 0)
        for role in ("analyst", "planner", "spec-reviewer"):
            self.assertEqual(r.shell("python harness/owner_tools.py approve doc=spec", agent=role)[0], 2, role)

    def test_init_writes_the_agents_and_doctor_reports(self):
        r = self.r
        for name in ("analyst", "spec-reviewer"):
            text = (r.root / ".claude/agents" / f"{name}.md").read_text(encoding="utf-8")
            self.assertIn(f"name: {name}", text)
            self.assertNotIn("{{", text)
        out = subprocess.run([sys.executable, str(LAOP), "doctor", str(r.root)], capture_output=True,
                             text=True, timeout=120).stdout
        self.assertIn("approvals: docs/spec.md: missing", out)
        self.assertIn("next: /laop discover", out)
        self.assertNotIn("UPGRADE AVAILABLE", out)
        lock = r.root / "harness" / "core.lock.json"
        data = json.loads(lock.read_text())
        data["files"]["harness/loop_core.py"] = "older"  # installed from an earlier core
        lock.write_text(json.dumps(data))
        out = subprocess.run([sys.executable, str(LAOP), "doctor", str(r.root)], capture_output=True,
                             text=True, timeout=120).stdout
        self.assertIn("UPGRADE AVAILABLE: 1 core file(s) differ from this skill's (harness/loop_core.py)", out)


# ============================================================================ resolver


class ResolverTests(HarnessTest):
    """Resolver modes, categories, the owner's decisions and the trust ledger."""

    MODE = "auto"

    def setUp(self):
        super().setUp()
        pj = json.loads((self.r.root / "harness" / "project.json").read_text())
        pj["resolver"]["mode"] = self.MODE
        self.r.commit_file("harness/project.json", json.dumps(pj), "config")
        self.r.tasks([task(), {"id": "G9", "title": "end", "agent": "owner-gate", "final": True,
                               "description": "x", "depends_on": ["M1-001"]}])

    def ask(self) -> dict:
        r = self.r
        r.step("step_next")
        r.commit_file("docs/open_questions.md", "# Q\n## Q1: which?\n", "M1-001: Q1")
        r.claim("M1-001", "blocked", question="Which option?", question_id="Q1", block_type="question")
        out = r.step("step_verify")
        self.assertIn("RESOLVE", out)
        self.assertIn("'resolver'", out)
        return r.state()["current"]

    def resolve(self, resolution: str, challenge: str) -> str:
        """Write the resolver's file, run the step, append the challenger's section, run it."""
        r = self.r
        cur = self.ask()
        (r.root / cur["resolution"]).write_text(resolution)
        out = r.step("step_resolve")
        self.assertIn("'challenger'", out)
        time.sleep(0.05)
        with open(r.root / cur["resolution"], "a") as f:
            f.write(f"\n{challenge}\nTried to break it.\n")
        return r.step("step_resolve")

    def test_autonomous_category_resumes_task_provisionally(self):
        r = self.r
        r.owner("trust", "category=D1", "level=auto")
        out = self.resolve("RESOLUTION: ANSWER\nCategory: D1\nDecision: option 1\n", "CHALLENGE: AGREE")
        self.assertIn("RESOLVED", out)
        st = r.state()
        self.assertEqual(st["tasks"]["M1-001"]["status"], "todo")
        self.assertEqual(st["tasks"]["M1-001"]["attempts"], 0)
        self.assertEqual(st["provisional"]["Q1"]["status"], "open")
        self.assertIn("DISPATCH", r.step("step_next"))
        prompt = (r.root / "harness" / "current_prompt.md").read_text()
        self.assertIn("Decision: option 1", prompt)
        self.assertIn("provisional", prompt)
        # finish the task: the final gate refuses while Q1 is open
        r.commit_file("src/b.py", "B = 2\n")
        r.claim("M1-001")
        r.step("step_verify")
        (r.root / r.state()["current"]["verdict"]).write_text("VERDICT: PASS\n")
        r.step("step_record")
        self.assertIn("PROVISIONAL", r.step("step_next"))
        r.owner("pass_gate", "task=G9")
        self.assertNotEqual(r.state()["tasks"]["G9"]["status"], "done")
        r.owner("ratify", "qid=Q1")
        r.owner("pass_gate", "task=G9")
        self.assertEqual(r.state()["tasks"]["G9"]["status"], "done")

    def test_amend_resumes_with_the_amendments(self):
        r = self.r
        r.owner("trust", "category=D1", "level=auto")
        out = self.resolve("RESOLUTION: ANSWER\nCategory: D1\nDecision: reject\n",
                           "CHALLENGE: AMEND\nAmendments: 1. compare float(value), not value.")
        self.assertIn("RESOLVED", out)
        r.step("step_next")
        prompt = (r.root / "harness" / "current_prompt.md").read_text()
        self.assertIn("compare float(value)", prompt)
        self.assertIn("every amendment", prompt)

    def test_warmup_category_goes_to_the_owner_with_the_recommendation(self):
        r = self.r
        out = self.resolve("RESOLUTION: ANSWER\nCategory: D1\nDecision: option 1\n", "CHALLENGE: AGREE")
        self.assertIn("FOR THE OWNER", out)
        st = r.state()
        self.assertEqual(st["tasks"]["M1-001"]["status"], "blocked")
        self.assertIn("warm-up", st["tasks"]["M1-001"]["blocked_reason"])
        self.assertEqual(st["questions"]["Q1"]["category"], "D1")
        self.assertIn("decide qid=Q1", (r.root / "harness" / "STATUS.md").read_text())
        self.assertIn("applied", r.owner("decide", "qid=Q1", "verdict=agree"))
        st = r.state()
        self.assertEqual(st["tasks"]["M1-001"]["status"], "todo")
        self.assertEqual(st["ledger"]["D1"]["streak"], 1)
        r.step("step_next")
        prompt = (r.root / "harness" / "current_prompt.md").read_text()
        self.assertIn("The owner decided your question", prompt)
        self.assertIn("Decision: option 1", prompt)

    def test_reserved_category_stays_with_the_owner(self):
        r = self.r
        out = self.resolve("RESOLUTION: ANSWER\nCategory: R1\nDecision: option 1\n", "CHALLENGE: AGREE")
        self.assertIn("FOR THE OWNER", out)
        self.assertIn("is the owner's", r.state()["tasks"]["M1-001"]["blocked_reason"])

    def test_unknown_category_stays_with_the_owner(self):
        r = self.r
        r.owner("trust", "category=D1", "level=auto")
        out = self.resolve("RESOLUTION: ANSWER\nCategory: X9\nDecision: option 1\n", "CHALLENGE: AGREE")
        self.assertIn("FOR THE OWNER", out)

    def test_escalation_still_gets_a_challenged_recommendation(self):
        r = self.r
        out = self.resolve("RESOLUTION: ESCALATE\nCategory: R3\nEscalated because: R3\nDecision: 2\n",
                           "CHALLENGE: AMEND\nAmendments: 1. also test zero.")
        self.assertIn("FOR THE OWNER", out)
        self.assertEqual(r.state()["questions"]["Q1"]["status"], "open")
        self.assertIn("applied",
                      r.owner("decide", "qid=Q1", "verdict=differ", "note=Use option 3.", "obvious=yes"))
        st = r.state()
        self.assertEqual(st["ledger"]["R3"]["obvious"], 1)
        self.assertEqual(st["ledger"]["R3"]["streak"], 0)
        self.assertIn("Use option 3", (r.root / "harness" / "STEER_M1-001.md").read_text())
        r.step("step_next")
        self.assertNotIn("Decision: 2", (r.root / "harness" / "current_prompt.md").read_text())

    def test_counter_goes_to_the_owner(self):
        out = self.resolve("RESOLUTION: ANSWER\nCategory: D1\nDecision: 1\n",
                           "CHALLENGE: COUNTER\nOption 2 is right.")
        self.assertIn("FOR THE OWNER", out)

    def test_resolver_cannot_plant_the_challenge(self):
        r = self.r
        cur = self.ask()
        (r.root / cur["resolution"]).write_text("RESOLUTION: ANSWER\nCategory: D1\nCHALLENGE: AGREE\n")
        r.step("step_resolve")
        self.assertIn("NO VALID CHALLENGER OUTPUT", r.step("step_resolve"))

    def test_override_reopens_the_task_and_demotes_the_category(self):
        r = self.r
        r.owner("trust", "category=D1", "level=auto")
        self.resolve("RESOLUTION: ANSWER\nCategory: D1\nDecision: 1\n", "CHALLENGE: AGREE")
        out = r.owner("override", "qid=Q1", "note=Use option 2.")
        self.assertIn("applied", out)
        st = r.state()
        self.assertEqual(st["provisional"]["Q1"]["status"], "overridden")
        self.assertEqual(st["ledger"]["D1"]["status"], "warmup")
        self.assertIn("option 2", (r.root / "harness" / "STEER_M1-001.md").read_text())

    def test_owner_action_question_is_not_resolved(self):
        r = self.r
        r.step("step_next")
        r.commit_file("src/b.py", "B = 2\n")
        r.claim("M1-001", "blocked", question="Please pass gate G1", block_type="owner_action")
        self.assertIn("is now 'blocked'", r.step("step_verify"))

    def test_placeholders_in_the_principles_keep_the_resolver_inactive(self):
        r = self.r
        path = r.root / "docs" / "owner_principles.md"
        r.commit_file("docs/owner_principles.md", path.read_text() + "\n(to be written by the owner)\n", "p")
        self.assertIn("WARNING", r.step("step_next"))
        r.commit_file("docs/open_questions.md", "# Q\n## Q1\n", "M1-001: Q1")
        r.claim("M1-001", "blocked", question="Which?", question_id="Q1", block_type="question")
        self.assertIn("is now 'blocked'", r.step("step_verify"))


class AdviseModeTests(ResolverTests):
    MODE = "advise"

    def test_autonomous_category_resumes_task_provisionally(self):
        r = self.r
        r.owner("trust", "category=D1", "level=auto")
        out = self.resolve("RESOLUTION: ANSWER\nCategory: D1\nDecision: option 1\n", "CHALLENGE: AGREE")
        self.assertIn("FOR THE OWNER", out)  # advise: the owner decides every question
        self.assertIn("mode advise", r.state()["tasks"]["M1-001"]["blocked_reason"])

    test_amend_resumes_with_the_amendments = None
    test_override_reopens_the_task_and_demotes_the_category = None
    test_warmup_category_goes_to_the_owner_with_the_recommendation = None
    test_reserved_category_stays_with_the_owner = None  # advise: every category is the owner's


class TrustLedgerTests(unittest.TestCase):
    """The promotion and demotion rules, on a bare state."""

    def setUp(self):
        sys.path.insert(0, str(SKILL / "core" / "harness"))
        import loop_core
        self.L = loop_core
        self._log, self._notify = loop_core.log, loop_core.notify
        loop_core.log = lambda *a, **k: None
        loop_core.notify = lambda *a, **k: None

    def tearDown(self):
        self.L.log, self.L.notify = self._log, self._notify

    def test_warmup_promotes_and_override_demotes(self):
        L = self.L
        state = {"events": []}
        self.assertEqual(L.category_status(state, "D2"), "warmup")
        self.assertEqual(L.category_status(state, "R1"), "owner")
        for i in range(L.C.RESOLVER_WARMUP - 1):
            L.ledger_record(state, "D2", f"Q{i}", "T", "agree")
        self.assertEqual(L.category_status(state, "D2"), "warmup")
        L.ledger_record(state, "D2", "Qx", "T", "differ")  # one difference restarts the warm-up
        for i in range(L.C.RESOLVER_WARMUP):
            L.ledger_record(state, "D2", f"Qy{i}", "T", "amend")
        self.assertEqual(L.category_status(state, "D2"), "auto")
        L.ledger_record(state, "D2", "Qz", "T", "overridden")
        self.assertEqual(L.category_status(state, "D2"), "warmup")

    def test_reserved_categories_are_never_promoted_automatically(self):
        L = self.L
        state = {"events": []}
        for i in range(10):
            L.ledger_record(state, "R4", f"Q{i}", "T", "agree")
        self.assertEqual(L.category_status(state, "R4"), "owner")
        for i in range(3):
            L.ledger_record(state, "R4", f"Qo{i}", "T", "obvious")
        self.assertTrue(L.trust_suggestions(state))


if __name__ == "__main__":
    unittest.main()
