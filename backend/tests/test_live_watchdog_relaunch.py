"""The watchdog must not stampede the live trader (2026-09-21).

Observed on a live Monday morning: the scheduled 09:25 launch was joined by copies
at 09:31, 09:36:17 and 09:36:39 — four ~15 GB market assemblies at once on a 31.6 GB
machine, free memory down to 1.1 GB. The cause is a timing gap in FQA, not in the
watchdog's intent:

    cli.py live → assemble market (minutes) → LiveTrader.run() → writes the pid file

The pid file therefore does not exist during assembly, ``live_trader_alive()`` said
"not running", and every 5-minute watchdog tick spawned another trader. Once the
copies reach ``run()`` they exit on the pid check, but the damage (memory, CPU) is
already done, and the real session is starved.

Two guards are pinned here: a process-table check for a launch in flight, and a
cooldown between launch attempts.
"""
from __future__ import annotations

import subprocess
import unittest
from datetime import datetime
from unittest import mock

from app.services import quant_manager as qm
from app.services import quant_scheduler as qs


class _FakeProc:
    def __init__(self, pid: int, cmdline: list[str]):
        self.info = {"pid": pid, "cmdline": cmdline}


class LiveTraderProcessScanTest(unittest.TestCase):
    def test_finds_only_live_traders(self):
        procs = [
            _FakeProc(1, ["python", "cli.py", "live"]),
            _FakeProc(2, ["python", "cli.py", "shadow"]),
            _FakeProc(3, ["python", "scripts/forward_health.py"]),
            _FakeProc(4, ["python.exe", "cli.py", "live"]),
            _FakeProc(5, []),
        ]
        with mock.patch("psutil.process_iter", lambda attrs=None: iter(procs)):
            self.assertEqual(sorted(qm.live_trader_processes()), [1, 4])

    def test_process_iter_errors_are_skipped(self):
        class _Boom:
            @property
            def info(self):
                raise OSError("process died")

        with mock.patch("psutil.process_iter", lambda attrs=None: iter([_Boom(), _FakeProc(7, ["python", "cli.py", "live"])])):
            self.assertEqual(qm.live_trader_processes(), [7])

    def test_alive_when_a_launch_is_in_flight_but_has_no_pid_file(self):
        """The gap that caused the stampede: assembling, so no pid file yet."""
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "outputs").mkdir(parents=True, exist_ok=True)
            with mock.patch.object(qm, "_project_root", lambda: str(root)), \
                 mock.patch.object(qm, "_live_account_name", lambda _r, _a: "D_5W"), \
                 mock.patch.object(qm, "live_trader_processes", lambda: [4321]):
                self.assertFalse((root / "outputs" / "live_D_5W.pid").exists())
                self.assertTrue(qm.live_trader_alive())

    def test_dead_with_no_pid_file_and_no_process(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "outputs").mkdir(parents=True, exist_ok=True)
            with mock.patch.object(qm, "_project_root", lambda: str(root)), \
                 mock.patch.object(qm, "_live_account_name", lambda _r, _a: "D_5W"), \
                 mock.patch.object(qm, "live_trader_processes", lambda: []):
                self.assertFalse(qm.live_trader_alive())


class WatchdogCooldownTest(unittest.TestCase):
    def setUp(self):
        self._saved = qs._live_last_launch_at
        qs._live_last_launch_at = 0.0
        self.addCleanup(self._restore)

    def _restore(self):
        qs._live_last_launch_at = self._saved

    def _at(self, hh: int, mm: int):
        class _DT(datetime):
            @classmethod
            def now(cls, tz=None):  # noqa: D102
                return datetime(2026, 9, 21, hh, mm, 0)

        return mock.patch.object(qs, "datetime", _DT)

    def test_second_launch_within_the_cooldown_is_suppressed(self):
        launch = mock.MagicMock(return_value={"ok": True, "pid": 1})
        with self._at(10, 0), \
             mock.patch.object(qs, "is_trading_day", lambda now=None: True), \
             mock.patch.object(qs.quant_manager, "live_trader_alive", lambda: False), \
             mock.patch.object(qs, "start_live_trader", launch):
            first = qs.live_watchdog()          # arms the cooldown
            second = qs.live_watchdog()         # 5 minutes later — must NOT launch
        launch.assert_called_once()
        self.assertTrue(first["ok"])
        self.assertIn("冷却", second["skipped"])

    def test_launch_resumes_after_the_cooldown(self):
        launch = mock.MagicMock(return_value={"ok": True, "pid": 1})
        qs._live_last_launch_at = 0.0
        with self._at(14, 0), \
             mock.patch.object(qs, "is_trading_day", lambda now=None: True), \
             mock.patch.object(qs.quant_manager, "live_trader_alive", lambda: False), \
             mock.patch.object(qs, "start_live_trader", launch):
            qs._live_last_launch_at = 1.0       # a very old attempt
            out = qs.live_watchdog()
        launch.assert_called_once()
        self.assertFalse(out["alive"])

    def test_start_live_trader_arms_the_cooldown(self):
        # the real function RETRIES with ``time.sleep(45)`` between attempts, so the
        # clock and the sleep are both neutralised: with a frozen clock the retry
        # window never closes and the test would hang (as the first version did).
        with mock.patch.object(qs, "is_trading_day", return_value=True), \
             mock.patch.object(qs, "_ensure_pit_db_or_fail",
                               return_value={"ok": False, "stage": "test"}), \
             mock.patch("time.sleep", lambda *_: None), \
             mock.patch.object(qs, "datetime") as fake_dt:
            fake_dt.now.return_value = datetime(2026, 9, 21, 14, 0, 0)
            qs.start_live_trader()               # fails fast, but must still arm it
        self.assertGreater(qs._live_last_launch_at, 0)

    def test_a_live_launch_in_flight_is_not_relaunched(self):
        """End-to-end shape of the 09-21 stampede: alive-by-process ⇒ skip."""
        launch = mock.MagicMock()
        with self._at(9, 36), \
             mock.patch.object(qs, "is_trading_day", lambda now=None: True), \
             mock.patch.object(qs.quant_manager, "live_trader_alive", lambda: True), \
             mock.patch.object(qs, "start_live_trader", launch):
            out = qs.live_watchdog()
        launch.assert_not_called()
        self.assertTrue(out["alive"])


if __name__ == "__main__":
    unittest.main()
