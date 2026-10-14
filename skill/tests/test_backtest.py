"""Deterministic parts of tools/backtest.py: hiding the decision, filtering the principles, the report."""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import backtest as B

ENTRY_BOLD = """
- Status: decided (owner, 2026-10-07)
- Raised by: builder M4-029, 2026-10-07

**Question.** Which tolerance?

**Recommendation.** Option 1.

**Decision (owner, 2026-10-07).** Option 2: keep C020 locked.
"""

ENTRY_STATUS = """
- Status: decided (owner, 2026-10-11): option 1, through Q85.
- Question: is (e) a mechanism?
- Recommendation: option 1.
"""


class Parsing(unittest.TestCase):
    def test_decision_is_found_and_removed(self):
        self.assertIn("Option 2", B.owner_decision(ENTRY_BOLD))
        stripped = B.strip_decision(ENTRY_BOLD)
        self.assertNotIn("Option 2", stripped)
        self.assertIn("- Status: open", stripped)
        self.assertIn("Recommendation", stripped)

    def test_decision_in_the_status_line(self):
        self.assertIn("option 1, through Q85", B.owner_decision(ENTRY_STATUS))
        self.assertNotIn("through Q85", B.strip_decision(ENTRY_STATUS))

    def test_principles_rows_from_later_questions_are_dropped(self):
        text = ("| P1 | early rule | Q10, Q40 | strong |\n| P2 | late rule | Q76 | weak |\n"
                "| N1 | stated principle | owner | - |\n")
        kept = B.filter_principles(text, 76)
        self.assertIn("early rule", kept)
        self.assertNotIn("late rule", kept)
        self.assertIn("stated principle", kept)

    def test_loop_agents(self):
        self.assertTrue(B.is_loop_agent("builder M45-006, 2026-10-11"))
        self.assertFalse(B.is_loop_agent("planner M4, 2026-10-04"))


class Report(unittest.TestCase):
    def setUp(self):
        self.out = Path(tempfile.mkdtemp(prefix="laop_bt_"))

    def tearDown(self):
        shutil.rmtree(self.out, ignore_errors=True)

    def add(self, qid: str, num: int, resolution: str, judge: str) -> dict:
        d = self.out / qid
        d.mkdir()
        (d / "resolution.md").write_text(resolution)
        (d / "judge.md").write_text(judge)
        return {"qid": qid, "num": num, "date": "2026-10-01", "raised_by": "builder", "dir": str(d),
                "resolution": str(d / "resolution.md"), "judge": str(d / "judge.md"), "classification": {}}

    def test_warmup_and_wrong_autonomous_decisions(self):
        agreed = "RESOLUTION: ANSWER\nCategory: D1\nDecision: x\n\nCHALLENGE: AGREE\n"
        manifest = [self.add(f"Q{i}", i, agreed, "MATCH: same\nOWNER_INFO: no\n") for i in range(1, 4)]
        manifest.append(self.add("Q4", 4, agreed, "MATCH: different\nOWNER_INFO: yes\n"))
        manifest.append(self.add("Q5", 5, "RESOLUTION: ESCALATE\nCategory: R1\n\nCHALLENGE: AGREE\n",
                                 "MATCH: same\nOWNER_INFO: yes\n"))
        (self.out / "manifest.json").write_text(json.dumps(manifest))
        B.report(self.out, warmup=3)
        text = (self.out / "report.md").read_text()
        self.assertIn("4 of 5: same as the owner 3, partial 0, **different 1**", text)
        self.assertIn("actually decided alone: 1 (wrong: 1)", text)  # Q4, after the warm-up Q1-Q3
        seed = json.loads((self.out / "ledger_seed.json").read_text())
        self.assertEqual(seed["D1"]["status"], "warmup")  # demoted by the wrong decision
        self.assertEqual(seed["R1"]["status"], "owner")
        self.assertIn("check that decision in the project: none", text)

    def test_defects_of_the_owner_decision_are_listed(self):
        manifest = [self.add("Q7", 7, "RESOLUTION: ESCALATE\nCategory: R8\n\nCHALLENGE: AMEND\n",
                             "MATCH: partial\nOWNER_INFO: no\nDEFECT: yes\n")]
        (self.out / "manifest.json").write_text(json.dumps(manifest))
        B.report(self.out, warmup=3)
        text = (self.out / "report.md").read_text()
        self.assertIn("check that decision in the project: Q7", text)
        self.assertIn("| partial | no | yes |", text)


if __name__ == "__main__":
    unittest.main()
