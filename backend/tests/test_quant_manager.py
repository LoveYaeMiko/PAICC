"""Pure-logic tests for the quant red-line pipeline.

Covers the pieces the FQA re-eval flagged as untested: ``_payload_timestamp``
(prefer the data's own run time over the poll time), ``redline_history`` (latest
N, ascending), and ``_append_redline_history`` (signature dedup that survives a
process restart). DB-touching helpers are exercised against mocked ``db`` calls
so nothing writes to the real ``paicc.db``.
"""

from __future__ import annotations

import unittest
from datetime import datetime
from unittest import mock

from app.services import quant_manager as qm


def _status(ts: float = 1_700_000_000.0, value: bool = False) -> dict:
    return {
        "timestamp": ts,
        "source": "shadow",
        "red_lines": [
            {"name": "cost_deviation", "label": "成本模型偏差", "level": "critical", "value": 405.98, "detail": ""},
            {"name": "short_leg_deviation", "label": "多空敞口失衡", "level": "ok", "value": 0.0, "detail": ""},
            {"name": "regime_switch", "label": "趋势切换", "level": "ok", "value": -1.85, "detail": ""},
            {"name": "pead_anomaly", "label": "PEAD 覆盖异常", "level": "ok", "value": value, "detail": ""},
        ],
    }


class PayloadTimestampTest(unittest.TestCase):
    def test_iso_last_run(self):
        iso = "2026-08-19T17:30:00"
        expected = datetime.fromisoformat(iso).timestamp()
        self.assertEqual(qm._payload_timestamp({"last_run": iso}), expected)

    def test_numeric_timestamp(self):
        self.assertEqual(qm._payload_timestamp({"timestamp": 1234.5}), 1234.5)

    def test_as_of_fallback(self):
        iso = "2026-08-18"
        expected = datetime.fromisoformat(iso).timestamp()
        self.assertEqual(qm._payload_timestamp({"as_of": iso}), expected)

    def test_missing_returns_none(self):
        self.assertIsNone(qm._payload_timestamp({}))
        self.assertIsNone(qm._payload_timestamp(None))
        self.assertIsNone(qm._payload_timestamp(["not", "a", "dict"]))

    def test_invalid_iso_returns_none(self):
        self.assertIsNone(qm._payload_timestamp({"last_run": "not-a-date"}))


class RedlineHistoryTest(unittest.TestCase):
    def test_latest_n_ascending_and_float_conversion(self):
        captured: dict = {}

        def fake_query(sql: str, params: tuple = ()) -> list[dict]:
            captured["sql"] = sql
            captured["params"] = params
            return [
                {"id": 1, "ts": 100.0, "source": "s", "name": "a", "label": "A", "level": "ok", "value": "123.45", "detail": ""},
                {"id": 2, "ts": 200.0, "source": "s", "name": "pead_anomaly", "label": "PEAD", "level": "ok", "value": "True", "detail": ""},
            ]

        with mock.patch.object(qm.db, "query", side_effect=fake_query):
            rows = qm.redline_history(limit=7)

        # Subquery must fetch the newest rows (ts DESC) before re-sorting ASC.
        self.assertIn("ORDER BY ts DESC", captured["sql"])
        self.assertEqual(captured["params"], (7,))
        # Numeric strings become floats; boolean strings stay raw.
        self.assertEqual(rows[0]["value"], 123.45)
        self.assertEqual(rows[1]["value"], "True")


class AppendRedlineHistoryTest(unittest.TestCase):
    def setUp(self):
        qm._last_history_signature = None

    def _signature_rows(self, status: dict) -> list[dict]:
        return [
            {"name": rl["name"], "level": rl["level"], "value": str(rl["value"])}
            for rl in status["red_lines"]
        ]

    def test_inserts_once_then_dedups(self):
        status = _status()
        with mock.patch.object(qm.db, "query", return_value=[]), \
             mock.patch.object(qm.db, "execute") as fake_exec:
            qm._append_redline_history(status)
            qm._append_redline_history(status)  # same signature -> skipped
        # Four red lines inserted exactly once.
        self.assertEqual(fake_exec.call_count, 4)

    def test_restart_seeds_signature_from_db(self):
        # Simulate a fresh process: in-memory signature is None, but the DB
        # already holds the same snapshot -> no duplicate writes.
        status = _status()
        with mock.patch.object(qm.db, "query", return_value=self._signature_rows(status)), \
             mock.patch.object(qm.db, "execute") as fake_exec:
            qm._append_redline_history(status)
        self.assertEqual(fake_exec.call_count, 0)

    def test_changed_value_inserts(self):
        status = _status()
        with mock.patch.object(qm.db, "query", return_value=[]), \
             mock.patch.object(qm.db, "execute"):
            qm._append_redline_history(status)
        changed = _status(value=True)  # pead_anomaly flips
        with mock.patch.object(qm.db, "query", return_value=[]), \
             mock.patch.object(qm.db, "execute") as fake_exec:
            qm._append_redline_history(changed)
        self.assertEqual(fake_exec.call_count, 4)


if __name__ == "__main__":
    unittest.main()
