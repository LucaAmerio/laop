"""Tests of laop monitor on a repository with the laop harness installed.

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

    def test_scheduled_stop_is_kept_and_created_when_due(self):
        reg = self.registry()
        rid, _ = reg.add(str(self.repo.root))
        repo, root = reg.get(rid), self.repo.root
        self.addCleanup(lambda: (root / "AGENT_STOP").unlink(missing_ok=True))
        now = self.viewer.time.time()
        self.assertIn("future", reg.schedule_stop([repo], "hard", now - 10))
        self.assertIn("kind", reg.schedule_stop([repo], "other", now + 60))
        self.assertIsNone(repo.stop_at)
        tag = repo.etag()
        self.assertEqual(reg.schedule_stop([repo], "hard", now + 3600), "")
        self.assertNotEqual(repo.etag(), tag)  # the page sees it at its next poll
        self.assertEqual(repo.snapshot()["stop_at"], {"kind": "hard", "at": now + 3600})
        self.assertEqual(self.viewer.Registry(reg.config).get(rid).stop_at, {"kind": "hard", "at": now + 3600})
        self.assertEqual(reg.fire_due(now), [])
        self.assertFalse((root / "AGENT_STOP").exists())
        self.assertEqual(reg.fire_due(now + 3600), [])
        self.assertIn("scheduled for", (root / "AGENT_STOP").read_text(encoding="utf-8"))
        self.assertIsNone(repo.stop_at)
        self.assertIsNone(self.viewer.Registry(reg.config).get(rid).stop_at)  # done: forgotten
        reg.schedule_stop([repo], "soft", now + 60)
        self.assertEqual(reg.schedule_stop([repo], None, None), "")  # cancelled
        self.assertIsNone(self.viewer.Registry(reg.config).get(rid).stop_at)

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

            def schedule(body, origin=base) -> int:
                return status(urllib.request.Request(base + "/api/stop/schedule", data=json.dumps(body).encode(),
                                                     method="POST", headers={"Content-Type": "application/json",
                                                                             "Origin": origin}))
            at = self.viewer.time.time() + 600
            self.assertEqual(schedule({"id": rid, "kind": "soft", "at": at}, "https://evil.example"), 403)
            self.assertEqual(schedule({"id": rid, "kind": "soft", "at": at - 1200}), 400)
            self.assertEqual(schedule({"id": "unknown", "kind": "soft", "at": at}), 404)
            self.assertEqual(schedule({"id": "all", "kind": "soft", "at": at}), 200)
            with urllib.request.urlopen(f"{base}/api/snapshot?repo={rid}") as r:
                self.assertEqual(json.loads(r.read())["stop_at"], {"kind": "soft", "at": at})
            self.assertEqual(schedule({"id": rid, "kind": None}), 200)
            self.assertIsNone(reg.get(rid).stop_at)

            def app_open(body) -> int:
                return status(urllib.request.Request(base + "/api/app/open", data=json.dumps(body).encode(),
                                                     method="POST", headers={"Content-Type": "application/json"}))
            self.assertEqual(app_open({"repo": rid}), 404)  # viewer.py: no window to bring forward
            opened = []
            handler.on_open = staticmethod(opened.append)
            self.assertEqual(app_open({"repo": rid}), 200)
            self.assertEqual(app_open({"repo": "unknown"}), 200)
            self.assertEqual(opened, [rid, ""])  # an unknown repository: the window only
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
        self.assertFalse(q1["provisional"] or q2["provisional"])
        prov, = self.viewer.parse_questions("## Q3: Cache\n\n- Status: provisional (resolver, 2026-10-12)\n\nText.\n")
        self.assertEqual((prov["open"], prov["provisional"]), (False, True))  # to ratify
        ratified, = self.viewer.parse_questions("## Q4: X\n\n- Status: decided (ratified by the owner, 2026-10-16; "
                                                "provisional decision of the resolver)\n")
        self.assertFalse(ratified["provisional"])
        self.assertNotIn("not a question", q2["body"])
        docs = self.repo.root / "docs"
        docs.mkdir(exist_ok=True)
        (docs / "open_questions.md").write_text(text, encoding="utf-8")
        snap = self.viewer.Repo(self.repo.root).snapshot()
        self.assertEqual([q["id"] for q in snap["questions_doc"]], ["Q1", "Q2"])
        v = self.viewer.Repo(self.repo.root)
        self.assertIsNone(v.decision("doc", "Q1"))  # decided: nothing to discuss
        self.assertEqual(v.decision("doc", "Q2")["file"], "docs/open_questions.md")
        (docs / "open_questions.md").write_text(text + "\n## Q0: Kept\n\n- Status: decided\n", encoding="utf-8")
        self.addCleanup((docs / "open_questions.md").write_text, text, encoding="utf-8")
        by_id = {q["id"]: q for q in self.viewer.Repo(self.repo.root).snapshot()["questions_doc"]}
        self.assertTrue(by_id["Q0"]["provisional"])  # its provisional decision is open in state.json
        self.assertFalse(by_id["Q1"]["provisional"])

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

    def test_a_decided_question_is_explained_never_applied(self):
        tmp = Path(tempfile.mkdtemp(prefix="laop_viewer_rev_"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / "harness").mkdir()
        (tmp / "harness" / "project.json").write_text('{"open_questions": "q.md"}', encoding="utf-8")
        (tmp / "q.md").write_text("## Q1: A\n\n- Status: decided\n\n**Decision.** (a), owner.\n", encoding="utf-8")
        repo = self.viewer.Repo(tmp)
        self.assertIsNone(repo.decision("doc", "Q1"))  # nothing to decide
        chats = self.viewer.Chats()
        old = self.viewer.claude_command
        self.viewer.claude_command = lambda: None
        self.addCleanup(setattr, self.viewer, "claude_command", old)
        chat, why = chats.start(repo, "rid", "doc", "Q1", False)
        self.assertEqual(why, "")
        self.assertTrue(chat.review)
        self.assertIn("Decided question Q1", chat.intro)
        self.assertNotIn("DECISION:", chat.system_prompt())
        chat._assistant([{"type": "text", "text": "It was (a).\nDECISION: (b)"}])
        self.assertEqual(chat.entries[-1]["text"], "It was (a).")
        self.assertNotIn("proposal", chat.entries[-1])
        chat.entries[-1]["proposal"] = "(b)"  # even so, nothing is written
        self.assertIn("no decision", chat.apply(len(chat.entries) - 1))
        self.assertIn("(a), owner.", (tmp / "q.md").read_text(encoding="utf-8"))

    def test_plan_usage_from_the_usage_command(self):
        now = self.viewer.dt.datetime(2026, 10, 30, 12, 0).timestamp()
        text = ("You are currently using your subscription\n\n"
                "Current session: 8% used · resets Oct 30, 2:30pm (Europe/Berlin)\n"
                "Current week (all models): 18% used · resets Nov 2, 9pm (Europe/Berlin)\n"
                "Current week (Fable): 0% used · resets Nov 2, 9pm (Europe/Berlin)\n")
        u = self.viewer.parse_usage(text, now)
        self.assertEqual((u["five_hour"]["used"], u["seven_day"]["used"]), (0.08, 0.18))
        from zoneinfo import ZoneInfo
        berlin = ZoneInfo("Europe/Berlin")
        self.assertEqual(u["five_hour"]["resets"], self.viewer.dt.datetime(2026, 10, 30, 14, 30, tzinfo=berlin).timestamp())
        self.assertEqual(u["seven_day"]["resets"], self.viewer.dt.datetime(2026, 11, 2, 21, 0, tzinfo=berlin).timestamp())
        dec = self.viewer.dt.datetime(2026, 12, 30, 12, 0).timestamp()
        self.assertEqual(self.viewer.dt.datetime.fromtimestamp(self.viewer.parse_reset("Jan 2, 9am", dec)).year, 2027)
        self.assertEqual(self.viewer.parse_usage("Not signed in", now), {})
        plan = self.viewer.PlanUsage(None)
        plan.set(u)
        self.assertEqual(plan.view()["seven_day"]["used"], 0.18)
        self.assertGreater(plan.wanted, 0)  # a page asked: the poller reads it every minute

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


FAKE_CLAUDE_CODE = r'''
import json, sys, time
log = sys.argv[1]
args, prompt = sys.argv[2:], sys.stdin.read()
with open(log, "a", encoding="utf-8") as f:
    f.write(json.dumps({"args": args, "prompt": prompt}) + "\n")
if "slow" in prompt:
    time.sleep(1.0)
out = lambda d: print(json.dumps(d), flush=True)
out({"type": "system", "subtype": "init", "session_id": "s-9", "model": "claude-opus-test"})
out({"type": "assistant", "parent_tool_use_id": None, "message": {"usage": {"input_tokens": 10,
     "cache_read_input_tokens": 50000, "cache_creation_input_tokens": 990}, "content": [
     {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "python harness/step_next.py"}}]}})
out({"type": "assistant", "parent_tool_use_id": "t1", "message": {"content": [{"type": "text", "text": "inside a subagent"}]}})
out({"type": "user", "parent_tool_use_id": None, "message": {"content": [{"type": "tool_result", "tool_use_id": "t1",
     "content": "Task T1 ready\nNEXT ACTION: spawn builder with harness/prompts/T1.md"}]}})
out({"type": "assistant", "parent_tool_use_id": None, "message": {"content": [{"type": "text", "text":
     "Five tasks are ready.\nSTART LOOP: run the ready tasks of M2"}]}})
out({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "unifiedWindows": {
     "five_hour": {"utilization": 0.38, "resetsAt": 4102444800}, "seven_day": {"utilization": 0.71, "resetsAt": 4102444800}}}})
out({"type": "result", "is_error": False, "result": "ok", "modelUsage": {"claude-opus-test": {"contextWindow": 200000}},
     "permission_denials": [{"tool_name": "Write", "tool_input": {}}]})
'''


class ConversationTest(unittest.TestCase):
    """Chats of a repository: Claude Code with the project's settings, the loop started from the page."""

    @classmethod
    def setUpClass(cls):
        cls.repo = Repo()
        cls.repo.tasks(TASKS)
        cls.viewer = load_viewer(cls.repo)
        cls.v = cls.viewer.Repo(cls.repo.root)

    @classmethod
    def tearDownClass(cls):
        cls.repo.close()

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="laop_viewer_conv_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        fake, self.log = self.tmp / "fake_claude.py", self.tmp / "calls.jsonl"
        fake.write_text(FAKE_CLAUDE_CODE, encoding="utf-8")
        old = self.viewer.claude_command
        self.viewer.claude_command = lambda: [sys.executable, str(fake), str(self.log)]
        self.addCleanup(setattr, self.viewer, "claude_command", old)

    def wait(self, conv):
        for _ in range(300):
            if not conv.busy:
                return
            threading.Event().wait(0.05)
        self.fail("the fake claude did not finish")

    def calls(self):
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def test_a_turn_runs_claude_code_with_the_project_settings(self):
        convs = self.viewer.Convs(self.tmp / "chats")
        conv, why = convs.new(self.v, "rid", {})
        self.assertEqual(why, "")
        self.assertEqual(conv.send("Where are we?"), "")
        self.wait(conv)
        self.assertEqual(conv.error, "")
        roles = [e["role"] for e in conv.entries]
        self.assertEqual(roles, ["user", "tool", "assistant", "denied"])  # nothing from inside the subagent
        tool = conv.entries[1]
        self.assertEqual(tool["text"], "Bash python harness/step_next.py")
        self.assertEqual(tool["result"], "NEXT ACTION: spawn builder with harness/prompts/T1.md")
        self.assertEqual(conv.entries[2]["text"], "Five tasks are ready.")
        self.assertEqual(conv.entries[2]["start_loop"], "run the ready tasks of M2")
        self.assertIn("Write", conv.entries[3]["text"])
        self.assertEqual(conv.context, {"used": 51000, "window": 200000})
        self.assertEqual((conv.session, conv.model_id, conv.title), ("s-9", "claude-opus-test", "Where are we?"))
        plan = convs.plan.view()
        self.assertEqual((plan["five_hour"]["used"], plan["seven_day"]["used"]), (0.38, 0.71))
        args = self.calls()[0]["args"]
        self.assertIn("--strict-mcp-config", args)
        self.assertNotIn("--setting-sources", args)  # the project's settings and hooks apply
        self.assertNotIn("--tools", args)
        self.assertEqual([args[args.index(k) + 1] for k in ("--model", "--effort", "--permission-mode")],
                         ["opus", "high", "acceptEdits"])
        self.assertNotIn("--resume", args)
        # the next turn resumes the session with the settings changed meanwhile
        self.assertEqual(conv.configure({"model": "sonnet", "mode": "manual"}), "")
        self.assertIn("must be one of", conv.configure({"effort": "huge"}))
        self.assertEqual(conv.send("And now?"), "")
        self.wait(conv)
        args = self.calls()[1]["args"]
        self.assertEqual([args[args.index(k) + 1] for k in ("--resume", "--model", "--permission-mode")],
                         ["s-9", "sonnet", "default"])
        # kept across a restart of the viewer
        again = self.viewer.Convs(self.tmp / "chats").get(conv.id)
        self.assertTrue(again.earlier)
        self.assertEqual((again.session, again.settings["model"], len(again.entries)),
                         ("s-9", "sonnet", len(conv.entries)))
        self.assertEqual(self.viewer.Convs(self.tmp / "chats").plan.view()["seven_day"]["used"], 0.71)

    def test_messages_sent_while_claude_works_wait(self):
        conv, _ = self.viewer.Convs(None).new(self.v, "rid", {})
        self.assertEqual(conv.send("slow one"), "")
        self.assertEqual(conv.send("second"), "")
        self.assertEqual(conv.send("third"), "")
        self.assertTrue(conv.entries[-1].get("queued"))
        self.wait(conv)
        self.assertEqual([c["prompt"] for c in self.calls()], ["slow one", "second\n\nthird"])
        self.assertFalse(any(e.get("queued") for e in conv.entries))

    def test_http_chat_and_start_loop(self):
        reg = self.viewer.Registry(self.tmp / "viewer.json")
        rid, _ = reg.add(str(self.repo.root))
        handler = type("H", (self.viewer.Handler,), {"registry": reg, "convs": self.viewer.Convs(self.tmp / "chats")})
        server = self.viewer.http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_address[1]}"

        def post(path, body):
            req = urllib.request.Request(base + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as exc:
                with exc:
                    return exc.code, json.loads(exc.read() or b"{}")

        code, conv = post("/api/conv/new", {"repo": rid, "model": "haiku"})
        self.assertEqual((code, conv["settings"]["model"]), (200, "haiku"))
        self.assertEqual(post("/api/conv/new", {"repo": rid, "model": "gpt"})[0], 400)
        cmd_file = self.repo.root / ".claude" / "commands" / "laop-start.md"
        saved = cmd_file.read_bytes() if cmd_file.exists() else b"loop"
        cmd_file.unlink(missing_ok=True)
        code, body = post("/api/conv/start_loop", {"id": conv["id"], "text": "rm -rf ."})
        self.assertEqual(code, 409)
        self.assertIn("laop-start.md", body["error"])
        cmd_file.parent.mkdir(parents=True, exist_ok=True)
        cmd_file.write_bytes(saved)
        self.assertEqual(post("/api/conv/start_loop", {"id": conv["id"], "text": "rm -rf ."})[0], 200)
        c = handler.convs.get(conv["id"])
        self.wait(c)
        self.assertEqual(self.calls()[0]["prompt"], "/laop-start")  # the server's command, never the page's text
        self.assertEqual(c.title, "Loop")
        with urllib.request.urlopen(f"{base}/api/convs?repo={rid}") as r:
            listing = json.loads(r.read())
        self.assertEqual([x["id"] for x in listing["convs"]], [conv["id"]])
        self.assertEqual(listing["plan"]["five_hour"]["used"], 0.38)
        with urllib.request.urlopen(f"{base}/api/conv?id={conv['id']}&since=1") as r:
            view = json.loads(r.read())
        self.assertEqual(view["entries"][0]["n"], 1)
        self.assertTrue(view["loop_command"])
        self.assertEqual(post("/api/conv/send", {"id": "nope", "text": "x"})[0], 404)
        self.assertEqual(post("/api/conv/remove", {"id": conv["id"]})[0], 200)
        self.assertIsNone(handler.convs.get(conv["id"]))


if __name__ == "__main__":
    unittest.main()
