"""Lint of the core files installed in projects: a project whose `ruff check .` selects RUF failed on
a list concatenation in guard.py (RUF005). Skipped when ruff is not installed."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path

CORE = Path(__file__).resolve().parents[1] / "core"


@unittest.skipUnless(importlib.util.find_spec("ruff"), "ruff is not installed")
class CoreLint(unittest.TestCase):
    def test_no_list_concatenation(self):
        files = sorted(str(p) for p in CORE.rglob("*.py") if "__pycache__" not in p.parts)
        result = subprocess.run(
            [sys.executable, "-m", "ruff", "check", "--isolated", "--no-cache", "--select", "RUF005",
             "--target-version", "py311", *files],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
