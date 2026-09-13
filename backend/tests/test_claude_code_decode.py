"""Claude Code file-watch diffs must survive non-ASCII content.

Reported 2026-09-13, from the backend log: every ~3 s the watcher died with

    UnicodeDecodeError: 'gbk' codec can't decode byte 0x80 in position 229
    AttributeError: 'NoneType' object has no attribute 'strip'   (claude_code.py:362)

The git helpers asked for ``text=True`` WITHOUT an encoding, so Python decoded
git's UTF-8 output as GBK (the default on Chinese Windows). The decode failure
happens in the subprocess reader thread, which leaves ``proc.stdout`` as ``None``
and the caller then calls ``.strip()`` on it. The session spawner in the same file
had already been fixed for this exact reason — the git helpers were missed.
"""
from __future__ import annotations

import subprocess
import unittest
from unittest import mock

from app.services import claude_code as cc


class GitDecodeTest(unittest.TestCase):
    def test_git_diff_requests_utf8(self):
        """The decode must be pinned to UTF-8 (git emits UTF-8, never GBK)."""
        captured: dict = {}

        def _fake_run(args, **kwargs):
            captured.update(kwargs)
            return subprocess.CompletedProcess(args, 0, stdout="+中文 diff\n", stderr="")

        with mock.patch.object(cc.subprocess, "run", _fake_run):
            out = cc._git_diff("/tmp/top", "docs/x.md", "/tmp/top/docs/x.md", "modified")

        self.assertEqual(captured.get("encoding"), "utf-8")
        self.assertEqual(captured.get("errors"), "replace")
        self.assertIn("中文", out)

    def test_git_diff_survives_a_dead_reader_thread(self):
        """stdout is None when the reader thread died — return None, never raise."""
        def _fake_run(args, **kwargs):
            return subprocess.CompletedProcess(args, 0, stdout=None, stderr=None)

        with mock.patch.object(cc.subprocess, "run", _fake_run):
            self.assertIsNone(cc._git_diff("/tmp/top", "a.py", "/tmp/top/a.py", "modified"))

    def test_git_top_level_requests_utf8_and_tolerates_none(self):
        captured: dict = {}

        def _fake_run(args, **kwargs):
            captured.update(kwargs)
            return subprocess.CompletedProcess(args, 0, stdout=None, stderr=None)

        cc._git_top_cache.clear()
        try:
            with mock.patch.object(cc.subprocess, "run", _fake_run):
                self.assertIsNone(cc._git_top_level("/tmp/whatever"))   # no crash
        finally:
            cc._git_top_cache.clear()
        self.assertEqual(captured.get("encoding"), "utf-8")
        self.assertEqual(captured.get("errors"), "replace")

    def test_emit_file_change_does_not_raise_when_the_diff_is_unavailable(self):
        with mock.patch.object(cc, "_git_top_level", lambda _r: "/tmp/top"), \
             mock.patch.object(cc, "_git_diff", lambda *_a: None), \
             mock.patch.object(cc, "publish") as pub:
            cc._emit_file_change("/tmp/top", "a.py", "modified")     # must not raise
        pub.assert_not_called()


if __name__ == "__main__":
    unittest.main()
