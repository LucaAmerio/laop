"""Agent frontmatter must be strict YAML. Claude Code tolerates an unquoted `description` that holds
": " (e.g. "VERDICT: PASS"), yaml.safe_load does not; projects that quoted it themselves then saw
core agents as modified locally and `upgrade` refused. Skipped when PyYAML is not installed."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
AGENT_DIRS = [SKILL / "core" / ".claude" / "agents", SKILL / "templates" / "agents"]


def agent_files() -> list[Path]:
    return sorted(p for d in AGENT_DIRS for p in d.rglob("*.md"))


def frontmatter(text: str) -> str:
    lines = text.lstrip("﻿").splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("no frontmatter")
    end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    return "\n".join(lines[1:end])


@unittest.skipUnless(importlib.util.find_spec("yaml"), "PyYAML is not installed")
class AgentFrontmatter(unittest.TestCase):
    def test_agents_found(self):
        self.assertGreater(len(agent_files()), 3)

    def test_frontmatter_is_strict_yaml(self):
        import yaml

        for path in agent_files():
            with self.subTest(agent=str(path.relative_to(SKILL))):
                data = yaml.safe_load(frontmatter(path.read_text(encoding="utf-8")))
                self.assertIsInstance(data, dict)
                self.assertIsInstance(data.get("name"), str)
                self.assertIsInstance(data.get("description"), str)
                self.assertTrue(data["description"].strip())


if __name__ == "__main__":
    unittest.main()
