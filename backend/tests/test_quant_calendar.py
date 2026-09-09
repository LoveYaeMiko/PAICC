"""Trading calendar + live watchdog (audit findings P-3 / P-4).

The scheduler used to gate every job on ``now.weekday() < 5``: holidays still
launched the live trader, the preclose order layer and the daily close loop, and
a trader that died mid-session was only noticed on the next backend restart.
"""
from __future__ import annotations

import unittest
from datetime import datetime
from unittest import mock

from app.services import quant_scheduler as qs
from app.services import trading_calendar as tc


class TradingCalendarTest(unittest.TestCase):
    def test_weekday_that_is_not_a_holiday(self):
        self.assertTrue(tc.is_trading_day("2026-09-09"))  # Wednesday

    def test_weekends_are_closed(self):
        self.assertFalse(tc.is_trading_day("2026-09-05"))  # Saturday
        self.assertFalse(tc.is_trading_day("2026-09-06"))  # Sunday

    def test_observed_2026_holidays_are_closed(self):
        for day in ("2026-01-01", "2026-02-17", "2026-04-06", "2026-05-04", "2026-06-19"):
            self.assertFalse(tc.is_trading_day(day), day)

    def test_operator_overrides_extend_the_list(self):
        self.assertTrue(tc.is_trading_day("2026-10-01", extra=""))
        self.assertFalse(tc.is_trading_day("2026-10-01", extra="2026-10-01,2026-10-02"))
        self.assertFalse(tc.is_trading_day("2026-10-02", extra="2026-10-01;2026-10-02"))
        # a malformed token is ignored rather than crashing the scheduler
        self.assertTrue(tc.is_trading_day("2026-10-05", extra="not-a-date"))

    def test_scheduler_helper_reads_settings(self):
        with mock.patch.object(qs.settings, "get", lambda key, default=None: "2026-10-01"):
            self.assertFalse(qs.is_trading_day(datetime(2026, 10, 1, 9, 25)))
            self.assertTrue(qs.is_trading_day(datetime(2026, 10, 2, 9, 25)))


class LiveWatchdogTest(unittest.TestCase):
    def test_noop_outside_the_session(self):
        with mock.patch.object(qs, "is_trading_day", lambda now=None: True), \
             mock.patch.object(qs, "datetime") as fake_dt:
            fake_dt.now.return_value = datetime(2026, 9, 9, 12, 30, 0)  # lunch break
            out = qs.live_watchdog()
        self.assertTrue(out["ok"])
        self.assertIn("skipped", out)

    def test_noop_on_a_holiday(self):
        with mock.patch.object(qs, "is_trading_day", lambda now=None: False):
            out = qs.live_watchdog()
        self.assertTrue(out["ok"])
        self.assertIn("skipped", out)

    def test_alive_trader_is_left_alone(self):
        with mock.patch.object(qs, "is_trading_day", lambda now=None: True), \
             mock.patch.object(qs, "datetime") as fake_dt, \
             mock.patch.object(qs.quant_manager, "live_trader_alive", lambda: True):
            fake_dt.now.return_value = datetime(2026, 9, 9, 10, 30, 0)
            out = qs.live_watchdog()
        assert out["ok"] is True
        assert out["alive"] is True
        assert out.get("ts")  # stamped so the panel can show last_run

    def test_dead_trader_is_relaunched(self):
        launch = mock.MagicMock(return_value={"ok": True, "pid": 1234})
        with mock.patch.object(qs, "is_trading_day", lambda now=None: True), \
             mock.patch.object(qs, "datetime") as fake_dt, \
             mock.patch.object(qs.quant_manager, "live_trader_alive", lambda: False), \
             mock.patch.object(qs, "start_live_trader", launch):
            fake_dt.now.return_value = datetime(2026, 9, 9, 14, 0, 0)
            out = qs.live_watchdog()
        self.assertTrue(out["ok"])
        self.assertFalse(out["alive"])
        launch.assert_called_once()


if __name__ == "__main__":
    unittest.main()
