"""Tests for the D-track closing-auction order list (``/quant/preclose``).

Covers the business reader (``quant_manager.read_preclose_orders``) against a
temporary FQA-root stub — normal parse, missing file, ``stale`` marking, account
name validation, corrupt output — plus the router's HTTP mapping (404 / 400 /
502) by calling the endpoint function directly.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from fastapi import HTTPException

from app.routers import quant as qr
from app.services import quant_manager as qm

#: Frozen "now" so ``stale`` / ``age_minutes`` are exact (2026-09-08 14:59:07).
_FROZEN_NOW = datetime(2026, 9, 8, 14, 59, 7)


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):  # noqa: D102 — test double
        return cls(*_FROZEN_NOW.timetuple()[:6])


class PrecloseOrdersTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "outputs").mkdir(parents=True, exist_ok=True)

        for name, value in (
            ("_project_root", lambda: str(self.root)),
            ("datetime", _FrozenDatetime),
        ):
            patcher = mock.patch.object(qm, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _write(self, account: str, payload) -> Path:
        path = self.root / "outputs" / f"preclose_orders_{account}.json"
        if isinstance(payload, str):
            path.write_text(payload, encoding="utf-8")
        else:
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def test_parses_orders_and_derives_freshness(self):
        self._write("D_5W", {
            "date": "2026-09-08",
            "ts": "14:54:07",
            "orders": [{"symbol": "601872.SH", "side": "buy", "shares": 300}],
            "note": "decided at 14:55 from 14:55-known data",
        })
        data = qm.read_preclose_orders(account="D_5W")
        self.assertIsNotNone(data)
        self.assertEqual(data["date"], "2026-09-08")
        self.assertEqual(data["ts"], "14:54:07")
        self.assertEqual(
            data["orders"], [{"symbol": "601872.SH", "side": "buy", "shares": 300}]
        )
        self.assertIn("14:55-known", data["note"])
        self.assertFalse(data["stale"])
        self.assertEqual(data["age_minutes"], 5)

    def test_missing_file_returns_none(self):
        self.assertIsNone(qm.read_preclose_orders(account="D_5W"))

    def test_old_date_is_marked_stale(self):
        self._write("D_5W", {"date": "2026-09-07", "ts": "14:54:07", "orders": []})
        data = qm.read_preclose_orders(account="D_5W")
        self.assertTrue(data["stale"])
        # 2026-09-07 14:54:07 -> 2026-09-08 14:59:07 == 24h5m
        self.assertEqual(data["age_minutes"], 1445)

    def test_missing_date_is_stale_and_falls_back_to_mtime(self):
        self._write("D_5W", {"orders": [], "note": "no date field"})
        data = qm.read_preclose_orders(account="D_5W")
        self.assertTrue(data["stale"])
        # mtime fallback uses the real clock, so a just-written file is 0 minutes old
        self.assertEqual(data["age_minutes"], 0)

    def test_missing_orders_becomes_empty_list(self):
        self._write("D_5W", {"date": "2026-09-08", "ts": "14:54:07"})
        data = qm.read_preclose_orders(account="D_5W")
        self.assertEqual(data["orders"], [])
        self.assertEqual(data["note"], "")

    def test_non_list_orders_is_ignored(self):
        self._write("D_5W", {"date": "2026-09-08", "orders": {"symbol": "x"}})
        self.assertEqual(qm.read_preclose_orders(account="D_5W")["orders"], [])

    def test_corrupt_output_raises(self):
        self._write("D_5W", "{not json at all")
        with self.assertRaises(qm.OutputCorruptError):
            qm.read_preclose_orders(account="D_5W")

    def test_explicit_account_selects_that_file(self):
        self._write("OTHER_1W", {"date": "2026-09-08", "ts": "14:50:00",
                                 "orders": [{"symbol": "000001.SZ", "side": "sell", "shares": 100}]})
        data = qm.read_preclose_orders(account="OTHER_1W")
        self.assertEqual(data["orders"][0]["symbol"], "000001.SZ")
        # D_5W has no file in this fixture.
        self.assertIsNone(qm.read_preclose_orders(account="D_5W"))

    def test_blank_account_resolves_from_live_config(self):
        self._write("D_5W", {"date": "2026-09-08", "orders": []})
        with mock.patch.object(qm, "_load_yaml", return_value={"live": {"account": "D_5W"}}):
            data = qm.read_preclose_orders(account="")
        self.assertIsNotNone(data)
        self.assertEqual(data["date"], "2026-09-08")

    def test_blank_account_defaults_to_d_5w_when_config_is_missing(self):
        self._write("D_5W", {"date": "2026-09-08", "orders": []})
        with mock.patch.object(qm, "_load_yaml", return_value=None):
            self.assertIsNotNone(qm.read_preclose_orders(account=""))

    def test_invalid_account_names_are_rejected(self):
        for bad in ("../evil", "..\\evil", "D-5W", "D 5W", "a/b", "D.5W", "D;5W",
                    "D_5W/../x", "C:\\abs", "D$5W", "*"):
            with self.subTest(account=bad):
                with self.assertRaises(ValueError):
                    qm.read_preclose_orders(account=bad)


class PrecloseEndpointTest(unittest.TestCase):
    """The endpoint must map the reader's outcomes onto HTTP status codes."""

    def test_returns_payload_on_success(self):
        payload = {"date": "2026-09-08", "ts": "14:54:07", "orders": [],
                   "note": "", "stale": False, "age_minutes": 5}
        with mock.patch.object(qr.quant_manager, "read_preclose_orders", return_value=payload):
            self.assertEqual(qr.preclose_orders(account="D_5W"), payload)

    def test_404_when_no_orders(self):
        with mock.patch.object(qr.quant_manager, "read_preclose_orders", return_value=None):
            with self.assertRaises(HTTPException) as ctx:
                qr.preclose_orders(account="D_5W")
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(ctx.exception.detail, "no preclose orders")

    def test_400_on_invalid_account(self):
        with mock.patch.object(
            qr.quant_manager, "read_preclose_orders",
            side_effect=ValueError("invalid account name: '../evil'"),
        ):
            with self.assertRaises(HTTPException) as ctx:
                qr.preclose_orders(account="../evil")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_502_on_corrupt_output(self):
        with mock.patch.object(
            qr.quant_manager, "read_preclose_orders",
            side_effect=qm.OutputCorruptError("FQA output 损坏或不可读"),
        ):
            with self.assertRaises(HTTPException) as ctx:
                qr.preclose_orders(account="D_5W")
        self.assertEqual(ctx.exception.status_code, 502)


class AccountNameValidationTest(unittest.TestCase):
    """Every per-account output reader must reject path-traversal names."""

    def test_read_live_status_rejects_traversal(self):
        for bad in ("../evil", "..\\evil", "D_5W/../../etc", "D 5W"):
            with self.assertRaises(ValueError):
                qm.read_live_status(account=bad)

    def test_read_trade_records_rejects_traversal(self):
        for bad in ("../evil", "..\\evil", "D_5W/../../etc", "D 5W"):
            with self.assertRaises(ValueError):
                qm.read_trade_records(account=bad)

    def test_plain_names_are_accepted(self):
        # Missing ledgers simply return empty payloads — no exception.
        self.assertEqual(qm.read_trade_records(account="D_5W")["account"], "D_5W")
        qm.read_live_status(account="D_5W")  # None or a dict, never ValueError


if __name__ == "__main__":
    unittest.main()
