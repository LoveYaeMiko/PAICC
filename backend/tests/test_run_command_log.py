"""A detached job's output must survive (2026-09-17: the live trader died silently).

``run_command`` used to spawn with ``stdout=DEVNULL, stderr=DEVNULL``. The live
trader is launched through it as a long-running detached process, so when it refused
to start five times in a row that day — the PIT store was unreachable, which makes
``cli.py live`` print ``ERROR: cannot reach PIT database ...`` and exit 2 BEFORE any
heartbeat or status file exists — the reason was thrown away. Nothing in the system
could say why the session never started.

These tests pin the replacement: an optional ``log_name`` writes the child's stdout
AND stderr into ``<project root>/outputs/<log_name>``, and ``live_health`` reads that
file back so the panel can show the last lines.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from app.services import quant_manager as qm


class RunCommandLogTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "outputs").mkdir(parents=True, exist_ok=True)
        patcher = mock.patch.object(qm, "_project_root", lambda: str(self.root))
        patcher.start()
        self.addCleanup(patcher.stop)
        active = mock.patch.object(qm, "_active_project_id", lambda: None)
        active.start()
        self.addCleanup(active.stop)
        db_patch = mock.patch.object(qm.db, "log_operation")
        self.log = db_patch.start()
        self.addCleanup(db_patch.stop)

    def _wait_for(self, path: Path, needle: str, seconds: float = 10.0) -> str:
        import time

        deadline = time.time() + seconds
        text = ""
        while time.time() < deadline:
            if path.is_file():
                text = path.read_text(encoding="utf-8", errors="replace")
                if needle in text:
                    return text
            time.sleep(0.2)
        return text

    def test_child_output_is_captured(self):
        # cmd.exe (shell=True): both streams must land in the same file
        qm.run_command(command="echo probe-stdout&& echo probe-stderr 1>&2",
                       log_name="probe.log")
        log = self.root / "outputs" / "probe.log"
        text = self._wait_for(log, "probe-stderr")
        self.assertIn("probe-stdout", text)
        self.assertIn("probe-stderr", text)
        # the operation log records WHERE the output went, so a dead job is traceable
        self.assertEqual(self.log.call_args.args[1]["log"], "probe.log")

    def test_without_log_name_nothing_is_written(self):
        qm.run_command(command="echo ignored")
        self.assertFalse((self.root / "outputs" / "probe.log").exists())
        self.assertNotIn("log", self.log.call_args.args[1])

    def test_unwritable_log_dir_falls_back_to_devnull(self):
        with mock.patch.object(Path, "open", side_effect=OSError("locked")):
            out = qm.run_command(command="echo fallback", log_name="locked.log")
        self.assertEqual(out["log"], "locked.log")     # reported, but no crash

    def test_live_start_uses_a_log_file(self):
        from app.services import quant_scheduler as qs

        with mock.patch.object(qs, "_ensure_pit_db_or_fail", return_value=None), \
             mock.patch.object(qs.quant_manager, "run_command",
                               return_value={"pid": 1, "log": "live_D_5W.log"}) as runner, \
             mock.patch.object(qs, "is_trading_day", return_value=True), \
             mock.patch.object(qs.db, "log_operation"):
            qs.start_live_trader()
        self.assertEqual(runner.call_args.kwargs.get("log_name"), "live_D_5W.log")


class LiveHealthLogTailTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "outputs").mkdir(parents=True, exist_ok=True)
        for target, attr, value in (
            (qm, "_project_root", lambda: str(self.root)),
            (qm, "_live_account_name", lambda _r, _a: "D_5W"),
            (qm, "live_trader_alive", lambda: False),
            (qm, "_pit_container_healthy", lambda: False),
        ):
            p = mock.patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)

        fixed = datetime(2026, 9, 17, 10, 30, 0)

        class _DT(datetime):
            @classmethod
            def now(cls, tz=None):  # noqa: D102
                return fixed

        p = mock.patch.object(qm, "datetime", _DT)
        p.start()
        self.addCleanup(p.stop)

    def test_the_traders_last_words_are_available(self):
        (self.root / "outputs" / "live_D_5W.log").write_text(
            "live: process priority -> HIGH\n"
            "ERROR: cannot reach PIT database (localhost:5432/pit_data): "
            "connection to server at \"localhost\" (127.0.0.1), port 5432 failed: "
            "Connection refused\n",
            encoding="utf-8",
        )
        out = qm.live_health()
        self.assertEqual(out["log_path"], "outputs/live_D_5W.log")
        self.assertTrue(any("cannot reach PIT database" in ln for ln in out["log_tail"]))
        self.assertEqual(out["status"], "down")
        self.assertIn("PIT", out["reason"])
        # the block stays JSON-serialisable for the API
        json.dumps(out)

    def test_no_log_file_is_not_an_error(self):
        out = qm.live_health()
        self.assertEqual(out["log_tail"], [])


if __name__ == "__main__":
    unittest.main()
