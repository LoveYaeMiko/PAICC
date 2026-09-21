"""Launch-time and resume guards for the real-time trader (2026-09-21).

Two findings from a live Monday morning:

1. **A 09:25 launch cannot reach the gate's availability floor.** Assembling the
   market slice takes ~8 minutes and ``LiveTrader.run()`` writes its first heartbeat
   only after that, so the trader cannot tick before ~09:33: a flawless day reaches
   237/240 = 98.75%, under the gate's 99% minimum. The launch now happens at 09:00
   (``quant_live_start_time``) so the assembly finishes before the open; the trader is
   forward-only and idles until the session starts, so starting early cannot trade a
   past timestamp.

2. **A backend restart during the session added a second trader.** The startup
   "live resume" path did not check whether one was already alive. The new copy cannot
   see the running one until it has finished its own assembly (FQA writes the pid lock
   inside ``run()``), so it costs a full ~15 GB build and then exits.
"""
from __future__ import annotations

import unittest
from datetime import datetime
from unittest import mock

from app.services import quant_scheduler as qs


class LiveResumeGuardTest(unittest.TestCase):
    def setUp(self):
        self._saved = qs._live_last_launch_at
        qs._live_last_launch_at = 0.0
        self.addCleanup(self._restore)

    def _restore(self):
        qs._live_last_launch_at = self._saved

    def test_resume_skips_when_a_trader_is_already_alive(self):
        with mock.patch.object(qs, "is_trading_day", lambda now=None: True), \
             mock.patch.object(qs.quant_manager, "live_trader_alive", lambda: True), \
             mock.patch.object(qs.threading, "Thread") as thread, \
             mock.patch.object(qs.db, "log_operation") as log:
            now = datetime(2026, 9, 21, 10, 30, 0)
            qs._live_resume(now)
        thread.assert_not_called()
        result = log.call_args.args[2]
        self.assertFalse(result["launched"])
        self.assertIn("已在运行", result["skipped"])

    def test_resume_launches_when_nothing_is_running(self):
        with mock.patch.object(qs, "is_trading_day", lambda now=None: True), \
             mock.patch.object(qs.quant_manager, "live_trader_alive", lambda: False), \
             mock.patch.object(qs.threading, "Thread") as thread, \
             mock.patch.object(qs.db, "log_operation") as log:
            now = datetime(2026, 9, 21, 10, 30, 0)
            qs._live_resume(now)
        thread.assert_called_once()
        self.assertTrue(log.call_args.args[2]["launched"])

    def test_resume_does_nothing_outside_the_session(self):
        with mock.patch.object(qs, "is_trading_day", lambda now=None: True), \
             mock.patch.object(qs.quant_manager, "live_trader_alive", lambda: False), \
             mock.patch.object(qs.threading, "Thread") as thread, \
             mock.patch.object(qs.db, "log_operation") as log:
            qs._live_resume(datetime(2026, 9, 21, 12, 0, 0))     # lunch break
        thread.assert_not_called()
        log.assert_not_called()

    def test_live_launch_is_before_the_open(self):
        """09:00 by default: the ~8-minute assembly must finish before 09:30."""
        from app.config import settings

        self.assertEqual(settings.get("quant_live_start_time"), "09:00")


if __name__ == "__main__":
    unittest.main()
