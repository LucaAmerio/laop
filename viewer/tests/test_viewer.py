"""Tests of laop_view on a repository with the laop harness installed.

The repository is built by the laop skill's test helper (tests/test_harness.py of the skill), found
in this repository (../skill) or at LAOP_SKILL.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import tempfile
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

VIEW = Path(__file__).resolve().parents[1]
SKILL = Path(os.environ.get("LAOP_SKILL", VIEW.parent / "skill"))
sys.path.insert(0, str(SKILL / "tests"))

from test_harness import Repo  # noqa: E402

TASKS = [
    {"id": "T1", "title": "First </script><b>x</b>", "agent": "builder", "description": "d",
     "acceptance": [{"cmd": ["python", "-c", "pass"]}], "review": False},
    {"id": "G1", "title": "Gate", "agent": "owner-gate", "depends_on": ["T1"], "final": True},
    {"id": "T2", "title": "Second", "agent": "builder", "depends_on": ["G1"],
     "acceptance": [{"cmd": ["python", "-c", "pass"]}], "review": False},
]


def load_viewer(repo: Repo):
    spec = importlib.util.spec_from_file_location("laop_viewer_under_test", VIEW / "viewer.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ViewerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo = Repo()
        cls.repo.tasks(TASKS)
        cls.out = cls.repo.step("step_next")
        cls.viewer = load_viewer(cls.repo)
        cls.v = cls.viewer.Repo(cls.repo.root)

    @classmethod
    def tearDownClass(cls):
        cls.repo.close()

    def test_snapshot_shows_the_task_in_flight_without_secrets(self):
        self.assertIn("DISPATCH", self.out)
        snap = self.v.snapshot()
        cur = snap["state"]["current"]
        self.assertEqual(cur["task_id"], "T1")
        self.assertNotIn("integrity", cur)
        self.assertNotIn("token_line", cur.get("spawn", {}))
        self.assertNotIn(self.repo.state()["current"]["spawn"]["token_line"], json.dumps(snap))
        tasks = {t["id"]: t for t in snap["tasks"]}
        self.assertTrue(tasks["T1"]["ready"])
        self.assertTrue(tasks["G1"]["gate"])
        self.assertEqual(tasks["G1"]["waiting_on"], ["T1"])
        self.assertEqual(tasks["T1"]["holds_up"], ["G1", "T2"])
        self.assertEqual(tasks["T1"]["dependents"], ["G1"])
        self.assertEqual(snap["problems"], [])

    def test_only_listed_files_are_served(self):
        (self.repo.root / "harness" / "verdicts").mkdir(exist_ok=True)
        (self.repo.root / "harness" / "verdicts" / "T1_attempt1.md").write_bytes(b"VERDICT: PASS\n")
        self.assertEqual(self.v.read_file("harness/verdicts/T1_attempt1.md"), "VERDICT: PASS\n")
        for rel in ("harness/state.json", "harness/project.json", "harness/verdicts/../state.json",
                    "../sibling/x.md", ".claude/settings.json", "src/a.py"):
            self.assertIsNone(self.v.read_file(rel), rel)

    def test_snapshot_page_is_self_contained_and_escaped(self):
        page = self.viewer.render_snapshot(self.v)
        self.assertIn("window.LAOP_SNAPSHOT = {", page)
        self.assertEqual(page.count("</script>"), 2)  # the data block and the page script only
        self.assertIn("<\\/script>", page)

    def test_task_times_from_the_log(self):
        log = [
            "[2026-10-12 10:00:00] dispatch A to builder (attempt 1)",
            "[2026-10-12 10:05:00] A asked Q1; resolution requested",
            "[2026-10-12 10:08:00] A attempt 1 rejected",
            "Progress: 0/3 done",
            "[2026-10-12 10:08:05] dispatch A to builder (attempt 2)",
            "[2026-10-12 10:10:00] A done (evaluator PASS)",
            "[2026-10-12 10:10:01] gate G ready for the owner",
            "[2026-10-12 10:10:02] dispatch B to builder (attempt 1)",
            "[2026-10-12 10:11:00] B checks passed, review requested",
            "[2026-10-12 10:12:00] loop finished: No runnable task.",
            "[2026-10-12 11:00:00] dispatch C to builder (attempt 1)",
            "[2026-10-12 11:02:00] C changed docs/open_questions.md",
            "[2026-10-12 11:30:00] G done (owner)",
        ]
        times = self.viewer.task_times(log, current_task=None)
        self.assertEqual(times["A"]["work"], 485 + 115)  # 10:00-10:08:05 and 10:08:05-10:10
        self.assertEqual(times["A"]["dispatches"], 2)
        self.assertEqual(times["A"]["done"] - times["A"]["first"], 600)
        self.assertEqual(times["B"]["work"], 118)  # closed by "loop finished"
        self.assertEqual(times["C"]["work"], 120)  # no closing line: up to its last mention
        self.assertEqual(times["G"]["done"] - times["G"]["ready"], 1 * 3600 + 19 * 60 + 59)
        running = self.viewer.task_times(log, current_task="C")
        self.assertEqual(running["C"]["work"], 0)
        self.assertIn("open_since", running["C"])

    def registry(self):
        tmp = Path(tempfile.mkdtemp(prefix="laop_viewer_cfg_"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        return self.viewer.Registry(tmp / "viewer.json")

    def test_registry_adds_checks_and_remembers_repositories(self):
        reg = self.registry()
        rid, why = reg.add(str(self.repo.root / "harness"))  # a sub-folder: its repository is taken
        self.assertEqual(why, "")
        self.assertEqual(reg.get(rid).root, self.repo.root.resolve())
        self.assertEqual(reg.add(str(self.repo.root))[0], rid)  # added once only
        self.assertIsNone(reg.add("relative/path")[0])
        self.assertIsNone(reg.add(str(self.repo.root / "nowhere"))[0])
        self.assertIn("No task loop", reg.add(str(self.repo.root / "src"))[1])
        again = self.viewer.Registry(reg.config)  # read back from the config file
        self.assertEqual([r["id"] for r in again.listing()], [rid])
        summary = again.overview()["repos"][0]
        self.assertEqual(summary["state"]["current"]["task_id"], "T1")
        self.assertNotIn("log", summary)
        self.assertNotIn("description", summary["tasks"][0])
        self.assertTrue(again.remove(rid))
        self.assertEqual(self.viewer.Registry(reg.config).listing(), [])

    def test_http_server(self):
        reg = self.registry()
        rid, _ = reg.add(str(self.repo.root))
        handler = type("H", (self.viewer.Handler,), {"registry": reg})
        server = self.viewer.http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"

        def status(req) -> int:
            try:
                with urllib.request.urlopen(req) as r:
                    return r.status
            except urllib.error.HTTPError as exc:
                exc.close()
                return exc.code

        try:
            with urllib.request.urlopen(base + "/") as r:
                self.assertIn(b"<title>", r.read())
            with urllib.request.urlopen(base + "/api/overview") as r:
                self.assertEqual(json.loads(r.read())["repos"][0]["id"], rid)
            with urllib.request.urlopen(f"{base}/api/snapshot?repo={rid}") as r:
                tag = r.headers["ETag"]
                self.assertEqual(json.loads(r.read())["state"]["current"]["task_id"], "T1")
            self.assertEqual(status(urllib.request.Request(f"{base}/api/snapshot?repo={rid}",
                                                           headers={"If-None-Match": tag})), 304)
            self.assertEqual(status(f"{base}/api/snapshot?repo=unknown"), 404)
            self.assertEqual(status(f"{base}/api/file?repo={rid}&path=harness/state.json"), 404)
            # another site in the same browser, or a rebound host name: refused
            rebound = urllib.request.Request(base + "/api/overview", headers={"Host": "evil.example"})
            self.assertEqual(status(rebound), 403)
            form = urllib.request.Request(base + "/api/repos/add", data=b"path=C:/x", method="POST",
                                          headers={"Content-Type": "application/x-www-form-urlencoded"})
            self.assertEqual(status(form), 403)
            cross = urllib.request.Request(base + "/api/repos/remove", data=json.dumps({"id": rid}).encode(),
                                           method="POST", headers={"Content-Type": "application/json",
                                                                   "Origin": "https://evil.example"})
            self.assertEqual(status(cross), 403)
            self.assertEqual(len(reg.listing()), 1)

            def stop(body, origin=base) -> int:
                return status(urllib.request.Request(base + "/api/stop", data=json.dumps(body).encode(), method="POST",
                                                     headers={"Content-Type": "application/json", "Origin": origin}))
            root = self.repo.root
            self.assertEqual(stop({"id": rid, "kind": "soft"}, "https://evil.example"), 403)
            self.assertFalse((root / "AGENT_SOFT_STOP").exists())
            self.assertEqual(stop({"id": rid, "kind": "soft"}), 200)
            self.assertTrue((root / "AGENT_SOFT_STOP").exists())
            self.assertEqual(stop({"id": "all", "kind": "hard"}), 200)
            self.assertTrue((root / "AGENT_STOP").exists())
            self.assertEqual(stop({"id": rid, "kind": "other"}), 400)
            self.assertEqual(stop({"id": "unknown", "kind": "hard"}), 404)
            self.assertEqual(stop({"id": "all", "kind": None}), 200)
            self.assertFalse((root / "AGENT_STOP").exists() or (root / "AGENT_SOFT_STOP").exists())
            own = urllib.request.Request(base + "/api/repos/remove", data=json.dumps({"id": rid}).encode(),
                                         method="POST", headers={"Content-Type": "application/json", "Origin": base})
            self.assertEqual(status(own), 200)
            self.assertEqual(reg.listing(), [])
        finally:
            server.shutdown()
            server.server_close()


FAKE_CLAUDE = r'''
import json, sys
log = sys.argv[1]
args, prompt = sys.argv[2:], sys.stdin.read()
with open(log, "a", encoding="utf-8") as f:
    f.write(json.dumps({"args": args, "prompt": prompt}) + "\n")
out = lambda d: print(json.dumps(d), flush=True)
out({"type": "system", "subtype": "init", "session_id": "s-1"})
out({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "docs/conf.py"}}]}})
out({"type": "assistant", "message": {"content": [{"type": "text", "text":
    "Markdown works with autodoc.\nDECISION: decide qid=Q1 verdict=amend note=\"MyST for user pages\""}]}})
out({"type": "result", "is_error": False, "result": "ok"})
'''


class DecisionsTest(unittest.TestCase):
    """Gates, questions and provisional decisions explained, and the read-only discussion."""

    @classmethod
    def setUpClass(cls):
        cls.repo = Repo()
        cls.repo.tasks(TASKS)
        cls.repo.step("step_next")
        h = cls.repo.root / "harness"
        (h / "resolutions").mkdir(exist_ok=True)
        (h / "resolutions" / "T2_Q1_1.md").write_text(
            "RESOLUTION: ESCALATE\nCategory: R2\n\nUse Markdown.\n\nCHALLENGE: AMEND\nKeep the API pages in reST.\n",
            encoding="utf-8")
        st = json.loads((h / "state.json").read_text(encoding="utf-8"))
        st["current"] = None
        st["tasks"]["T1"] = {"status": "done", "attempts": 1}
        st["tasks"]["T2"] = {"status": "blocked", "block_type": "question",
                             "blocked_reason": "Q1: Markdown or reST? [the resolver escalated; see x] Decide: ..."}
        st["questions"] = {"Q1": {"task": "T2", "file": "harness/resolutions/T2_Q1_1.md", "status": "open",
                                  "category": "R2", "why": "the resolver escalated", "time": "2026-10-12 10:00:00"}}
        st["provisional"] = {"Q0": {"task": "T1", "file": "harness/resolutions/missing.md", "status": "open"},
                             "Qx": {"task": "T1", "file": "x", "status": "ratified"}}
        (h / "state.json").write_text(json.dumps(st), encoding="utf-8")
        cls.viewer = load_viewer(cls.repo)
        cls.v = cls.viewer.Repo(cls.repo.root)

    @classmethod
    def tearDownClass(cls):
        cls.repo.close()

    def test_decisions_are_explained(self):
        items = {(d["kind"], d["id"]): d for d in self.v.snapshot()["decisions"]}
        self.assertEqual(set(items), {("gate", "G1"), ("question", "Q1"), ("provisional", "Q0")})
        q = items["question", "Q1"]
        self.assertEqual(q["question"], "Markdown or reST?")
        self.assertEqual((q["resolver_verdict"], q["resolver"]), ("ESCALATE", "Use Markdown."))
        self.assertEqual((q["challenge_verdict"], q["challenger"]), ("AMEND", "Keep the API pages in reST."))
        self.assertEqual(items["gate", "G1"]["holds_up"], ["T2"])
        self.assertNotIn("resolver", items["provisional", "Q0"])  # its file is missing: nothing invented
        self.assertNotIn("decisions", self.v.summary())

    def test_questions_file_entries_open_and_closed(self):
        text = ("# Open questions\n\nIntro.\n\n## Q1: Endpoint\n\n- Status: decided\n- Raised by: planner\n"
                "- Blocks: T1\n\n**Question.** Which one?\n\n**Decision.** (a), owner.\n\n"
                "## Q2: Units\n\n- Status: partly decided (`keep`); item 2 open\n\nText.\n\n**Decision.** _(owner)_\n\n"
                "## Notes\n\nnot a question\n")
        q1, q2 = self.viewer.parse_questions(text)
        self.assertEqual((q1["id"], q1["title"], q1["status"], q1["open"]), ("Q1", "Endpoint", "decided", False))
        self.assertEqual((q1["raised_by"], q1["blocks"], q1["decision"]), ("planner", "T1", "(a), owner."))
        self.assertTrue(q1["body"].startswith("**Question.**"))
        self.assertTrue(q2["open"])
        self.assertNotIn("not a question", q2["body"])
        docs = self.repo.root / "docs"
        docs.mkdir(exist_ok=True)
        (docs / "open_questions.md").write_text(text, encoding="utf-8")
        snap = self.viewer.Repo(self.repo.root).snapshot()
        self.assertEqual([q["id"] for q in snap["questions_doc"]], ["Q1", "Q2"])
        v = self.viewer.Repo(self.repo.root)
        self.assertIsNone(v.decision("doc", "Q1"))  # decided: nothing to discuss
        self.assertEqual(v.decision("doc", "Q2")["file"], "docs/open_questions.md")

    def test_write_decision_changes_only_that_entry(self):
        tmp = Path(tempfile.mkdtemp(prefix="laop_viewer_q_"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / "harness").mkdir()
        (tmp / "harness" / "project.json").write_text('{"open_questions": "q.md"}', encoding="utf-8")
        text = ("# Q\r\n\r\n## Q1: A\r\n\r\n- Status: open\r\n\r\n**Question.** x?\r\n\r\n**Decision.** _(owner,\r\n"
                "at gate)_\r\n\r\n**Note.** stays\r\n\r\n## Q2: B\r\n\r\n- Status: open\r\n\r\n**Decision.** _(owner)_\r\n")
        (tmp / "q.md").write_bytes(text.encode("utf-8"))
        repo = self.viewer.Repo(tmp)
        ok, out = self.viewer.write_decision(repo, "Q1", "(a), owner,\n 2026-10-13.")
        self.assertTrue(ok, out)
        self.assertEqual((tmp / "q.md").read_bytes().decode("utf-8"), text.replace(
            "- Status: open\r\n\r\n**Question.**", "- Status: decided\r\n\r\n**Question.**").replace(
            "**Decision.** _(owner,\r\nat gate)_", "**Decision.** (a), owner, 2026-10-13."))
        self.assertFalse(self.viewer.write_decision(repo, "Q9", "x")[0])

    def test_apply_runs_only_the_checked_decision(self):
        chats = self.viewer.Chats()
        old = self.viewer.claude_command
        self.viewer.claude_command = lambda: None  # no turn runs: the entries are added by hand
        self.addCleanup(setattr, self.viewer, "claude_command", old)
        chat, _ = chats.start(self.v, "rid", "question", "Q1", False)
        chat.add("assistant", "ok", command='python harness/owner_tools.py decide qid=Q1 verdict=amend note="a b"')
        chat.add("assistant", "no decision here")
        ran = []
        run = self.viewer.run_owner_tool
        self.viewer.run_owner_tool = lambda repo, command: (ran.append(command), (True, "done"))[1]
        self.addCleanup(setattr, self.viewer, "run_owner_tool", run)
        self.assertIn("no decision", chat.apply(1))
        self.assertEqual(chat.apply(0), "")
        self.assertEqual(ran, ['python harness/owner_tools.py decide qid=Q1 verdict=amend note="a b"'])
        self.assertEqual(chat.apply(0), "Already applied.")
        self.assertEqual(chat.entries[-1]["role"], "applied")
        self.assertIs(chats.find("rid", "question", "Q1"), chat)
        self.assertEqual(self.viewer.shlex.split('decide qid=Q1 verdict=amend note="a b"')[-1], "note=a b")

    def test_applying_a_doc_decision_unblocks_the_waiting_task(self):
        tmp = Path(tempfile.mkdtemp(prefix="laop_viewer_unb_"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / "harness").mkdir()
        (tmp / "harness" / "project.json").write_text('{"open_questions": "q.md"}', encoding="utf-8")
        (tmp / "harness" / "tasks.json").write_text(
            '{"tasks": [{"id": "T1", "agent": "builder"}, {"id": "T2", "agent": "builder"}]}', encoding="utf-8")
        (tmp / "harness" / "state.json").write_text(
            '{"tasks": {"T1": {"status": "blocked", "block_type": "question", "blocked_reason": "Q1: x?"},'
            ' "T2": {"status": "blocked", "block_type": "question", "blocked_reason": "Q2: y?"}}}', encoding="utf-8")
        (tmp / "q.md").write_text("## Q1: A\n\n- Status: open\n\n**Decision.** _(owner)_\n", encoding="utf-8")
        repo = self.viewer.Repo(tmp)
        self.assertEqual(self.viewer.tasks_blocked_on(repo, "Q1"), ["T1"])
        chats = self.viewer.Chats()
        old = self.viewer.claude_command
        self.viewer.claude_command = lambda: None
        self.addCleanup(setattr, self.viewer, "claude_command", old)
        chat, _ = chats.start(repo, "rid", "doc", "Q1", False)
        chat.add("assistant", "ok", proposal="(a)")
        ran = []
        run = self.viewer.run_owner_tool
        self.viewer.run_owner_tool = lambda r, command: (ran.append(command), (True, "done"))[1]
        self.addCleanup(setattr, self.viewer, "run_owner_tool", run)
        self.assertEqual(chat.apply(0), "")
        self.assertEqual(ran, ["python harness/owner_tools.py unblock task=T1"])

    def test_proposed_commands_are_checked(self):
        pc = self.viewer.proposed_command
        self.assertEqual(pc("question", "Q1", 'x\nDECISION: decide qid=Q1 verdict=agree'),
                         "python harness/owner_tools.py decide qid=Q1 verdict=agree")
        self.assertIsNone(pc("question", "Q1", "DECISION: decide qid=Q2 verdict=agree"))  # another item
        self.assertIsNone(pc("question", "Q1", 'DECISION: decide qid=Q1 verdict=agree note="a $(rm x)"'))
        self.assertIsNone(pc("gate", "G1", "DECISION: pass_gate task=G1; rm -rf /"))
        self.assertEqual(pc("provisional", "Q0", 'DECISION: override qid=Q0 note="no"'),
                         'python harness/owner_tools.py override qid=Q0 note="no"')

    def test_discussion_runs_claude_read_only(self):
        tmp = Path(tempfile.mkdtemp(prefix="laop_viewer_chat_"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        fake, log = tmp / "fake_claude.py", tmp / "calls.jsonl"
        fake.write_text(FAKE_CLAUDE, encoding="utf-8")
        old = self.viewer.claude_command
        self.viewer.claude_command = lambda: [sys.executable, str(fake), str(log)]
        self.addCleanup(setattr, self.viewer, "claude_command", old)

        def wait(chat):
            for _ in range(200):
                if not chat.busy:
                    return
                threading.Event().wait(0.05)
            self.fail("the fake claude did not finish")

        chats = self.viewer.Chats()
        chat, why = chats.start(self.v, "rid", "question", "Q1", False)
        self.assertEqual(why, "")
        wait(chat)
        self.assertEqual(chat.error, "")
        roles = [(e["role"], e["text"]) for e in chat.entries]
        self.assertEqual(roles[0], ("tool", "Read docs/conf.py"))
        self.assertEqual(chat.entries[1]["text"], "Markdown works with autodoc.")
        self.assertEqual(chat.entries[1]["command"],
                         'python harness/owner_tools.py decide qid=Q1 verdict=amend note="MyST for user pages"')
        self.assertIs(chats.start(self.v, "rid", "question", "Q1", False)[0], chat)  # the same discussion
        self.assertEqual(chat.send("And the API pages?"), "")
        wait(chat)
        calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        first, second = calls
        self.assertIn("Markdown or reST?", first["prompt"])
        self.assertIn("harness/resolutions/T2_Q1_1.md", first["prompt"])
        self.assertEqual(first["args"][first["args"].index("--tools") + 1], "Read,Grep,Glob")
        self.assertIn("--strict-mcp-config", first["args"])
        self.assertEqual(first["args"][first["args"].index("--setting-sources") + 1], "user")
        self.assertNotIn("--resume", first["args"])
        self.assertEqual(second["args"][second["args"].index("--resume") + 1], "s-1")
        self.assertEqual(second["prompt"], "And the API pages?")
        self.assertIsNone(chats.start(self.v, "rid", "question", "nope", False)[0])


if __name__ == "__main__":
    unittest.main()
