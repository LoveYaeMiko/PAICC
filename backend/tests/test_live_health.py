"""``quant_manager.live_health`` — the "did the live layer run today?" verdict.

Reported 2026-09-17: Docker/PIT were down, ``cli.py live`` was launched at 09:25 and
relaunched four times, the session produced ZERO heartbeats — and the panel showed
nothing, because ``outputs/live_D_5W.json`` only exists once the trader completes a
poll. The health block is derived from the pid lock and the heartbeat jsonl, so a
fully missed session is now visible instead of silent.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from app.services import quant_manager as qm


def _hb(ts: str) -> str:
    return json.dumps({"ts": ts, "equity_live": 62278.45, "n_positions": 0})


class LiveHealthTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "outputs").mkdir(parents=True, exist_ok=True)
        for target in (qm,):
            patcher = mock.patch.object(target, "_project_root", lambda: str(self.root))
            patcher.start()
            self.addCleanup(patcher.stop)
        # the account name comes from the real config; pin it for the temp root
        acct = mock.patch.object(qm, "_live_account_name", lambda _r, _a: "D_5W")
        acct.start()
        self.addCleanup(acct.stop)
        pit = mock.patch.object(qm, "_pit_container_healthy", lambda: True)
        pit.start()
        self.addCleanup(pit.stop)

    def _heartbeats(self, rows: list[str]) -> None:
        (self.root / "outputs" / "live_D_5W.jsonl").write_text(
            "\n".join(rows) + "\n", encoding="utf-8"
        )

    def _at(self, hh: int, mm: int):
        """Freeze ``datetime.now`` inside quant_manager to a fixed wall clock."""
        fixed = datetime(2026, 9, 17, hh, mm, 0)

        class _DT(datetime):
            @classmethod
            def now(cls, tz=None):  # noqa: D102
                return fixed

        return mock.patch.object(qm, "datetime", _DT)

    def test_in_session_with_a_dead_trader_is_down(self):
        with self._at(10, 5), mock.patch.object(qm, "live_trader_alive", lambda: False):
            out = qm.live_health()
        self.assertEqual(out["status"], "down")
        self.assertTrue(out["in_session"])
        self.assertFalse(out["alive"])
        self.assertIn("实时进程未运行", out["reason"])
        self.assertIn("10:05", out["reason"])

    def test_down_reason_names_the_pit_dependency(self):
        with self._at(10, 5), \
             mock.patch.object(qm, "live_trader_alive", lambda: False), \
             mock.patch.object(qm, "_pit_container_healthy", lambda: False):
            out = qm.live_health()
        self.assertEqual(out["status"], "down")
        self.assertIn("PIT", out["reason"])

    def test_alive_and_ticking_is_ok(self):
        rows = [_hb(f"2026-09-17 09:{m:02d}:11") for m in range(30, 60)]
        rows += [_hb(f"2026-09-17 10:{m:02d}:11") for m in range(0, 6)]
        self._heartbeats(rows)
        with self._at(10, 5), mock.patch.object(qm, "live_trader_alive", lambda: True):
            out = qm.live_health()
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["ticks_today"], 36)        # 09:30:11 .. 10:05:11
        self.assertEqual(out["expected_ticks_today"], 35)   # 09:30..10:05
        self.assertGreaterEqual(out["coverage_today"], 1.0)
        self.assertEqual(out["last_heartbeat"], "2026-09-17 10:05:11")

    def test_a_day_with_no_heartbeats_is_late_after_the_close(self):
        self._heartbeats([_hb("2026-09-16 09:30:11")])   # yesterday only
        with self._at(16, 30), mock.patch.object(qm, "live_trader_alive", lambda: False):
            out = qm.live_health()
        self.assertEqual(out["status"], "late")
        self.assertEqual(out["ticks_today"], 0)
        self.assertEqual(out["expected_ticks_today"], 240)
        self.assertIn("全天未运行", out["reason"])

    def test_partial_coverage_is_late(self):
        # 100 of 240 ticks: the trader started ~1.5 h late (the 2026-09-15 shape)
        rows = [_hb(f"2026-09-17 11:{m:02d}:11") for m in range(0, 30)]
        rows += [_hb(f"2026-09-17 13:{m:02d}:11") for m in range(0, 60)]
        rows += [_hb(f"2026-09-17 14:{m:02d}:11") for m in range(0, 10)]
        self._heartbeats(rows)
        with self._at(16, 30), mock.patch.object(qm, "live_trader_alive", lambda: False):
            out = qm.live_health()
        self.assertEqual(out["status"], "late")
        self.assertEqual(out["ticks_today"], 100)
        self.assertAlmostEqual(out["coverage_today"], 0.4167, places=3)

    def test_outside_the_session_nothing_is_expected(self):
        with self._at(8, 0), mock.patch.object(qm, "live_trader_alive", lambda: False):
            out = qm.live_health()
        self.assertFalse(out["in_session"])
        self.assertEqual(out["expected_ticks_today"], 0)
        self.assertEqual(out["status"], "ok")

    def test_lunch_break_is_not_downtime(self):
        self.assertTrue(qm._live_in_session(datetime(2026, 9, 17, 9, 30)))
        self.assertTrue(qm._live_in_session(datetime(2026, 9, 17, 11, 29)))
        self.assertFalse(qm._live_in_session(datetime(2026, 9, 17, 11, 45)))
        self.assertTrue(qm._live_in_session(datetime(2026, 9, 17, 13, 0)))
        self.assertFalse(qm._live_in_session(datetime(2026, 9, 17, 15, 1)))
        # 09:30-11:30 + 13:00-15:00 = 240 minutes for a full session
        self.assertEqual(qm._live_window_minutes(datetime(2026, 9, 17, 16, 30)), 240)

    def test_missing_heartbeat_file_is_not_an_error(self):
        with self._at(16, 30), mock.patch.object(qm, "live_trader_alive", lambda: False):
            out = qm.live_health()
        self.assertIsNone(out["last_heartbeat"])
        self.assertEqual(out["ticks_today"], 0)


class LiveRouterHealthTest(unittest.TestCase):
    def test_router_returns_a_payload_even_without_a_status_file(self):
        from app.routers import quant as qr

        health = {"account": "D_5W", "status": "down", "reason": "盘中实时进程未运行"}
        with mock.patch.object(qm, "read_live_status", lambda account="": None), \
             mock.patch.object(qm, "live_health", lambda account="": health):
            out = qr.live_status()
        self.assertEqual(out["health"], health)

    def test_router_merges_health_into_the_status(self):
        from app.routers import quant as qr

        health = {"account": "D_5W", "status": "ok"}
        with mock.patch.object(qm, "read_live_status", lambda account="": {"ts": "2026-09-17 10:00:11"}), \
             mock.patch.object(qm, "live_health", lambda account="": health):
            out = qr.live_status()
        self.assertEqual(out["ts"], "2026-09-17 10:00:11")
        self.assertEqual(out["health"], health)


if __name__ == "__main__":
    unittest.main()
