"""Digest of git-ignored integrity paths (found in a real project: byte-code caches written on import
of a package halted the loop)."""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core" / "harness"))

import loop_core as L  # noqa: E402


class TreeDigest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="laop_digest_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_site_packages_ignores_caches_and_sees_installs(self):
        sp = self.tmp / "site-packages"
        (sp / "pkg").mkdir(parents=True)
        (sp / "pkg" / "__init__.py").write_text("")
        (sp / "pkg-1.0.dist-info").mkdir()
        (sp / "_editable.pth").write_text("/src\n")
        before = L._tree_digest(sp)
        (sp / "pkg" / "__pycache__").mkdir()
        (sp / "pkg" / "__pycache__" / "__init__.cpython-314.pyc").write_bytes(b"x")
        self.assertEqual(L._tree_digest(sp), before)
        (sp / "other-2.0.dist-info").mkdir()
        self.assertNotEqual(L._tree_digest(sp), before)
        shutil.rmtree(sp / "other-2.0.dist-info")
        (sp / "_editable.pth").write_text("/elsewhere\n")
        self.assertNotEqual(L._tree_digest(sp), before)

    def test_plain_directory_ignores_caches(self):
        d = self.tmp / "data"
        d.mkdir()
        (d / "a.csv").write_text("1\n")
        before = L._tree_digest(d)
        (d / ".pytest_cache").mkdir()
        (d / ".pytest_cache" / "v").write_text("x")
        (d / "b.pyc").write_bytes(b"x")
        self.assertEqual(L._tree_digest(d), before)
        (d / "a.csv").write_text("2, changed\n")
        self.assertNotEqual(L._tree_digest(d), before)


if __name__ == "__main__":
    unittest.main()
