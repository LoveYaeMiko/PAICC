"""Tests for the forward-period panel feed (``/quant/forward``).

Covers the business readers against a temporary FQA-root stub — the newest
``forward_health*`` / ``paired_*`` artifact wins, pre-registration records are
listed newest-first, a corrupt artifact raises instead of silently reporting
"never ran", and the router maps a missing/valid payload as documented.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException

from app.routers import quant as qr
from app.services import quant_manager as qm


def _health(verdict: str = "fail", failed: list[str] | None = None) -> dict:
    return {
        "account": "D_5W",
        "window": {"start": "2026-09-01", "end": "2026-09-09"},
        "n_fills": 6,
        "gate": {
            "hard": {
                "tracking_error_daily_pp": {"ok": verdict == "pass", "value": 0.99,
                                            "max": 0.2, "n_days": 1, "min_days": 5},
                "availability": {"ok": True, "value": 0.995, "min": 0.99},
            },
            "soft": {"record_only": ["sharpe"], "values": {"sharpe": 5.9},
                     "note": "no forward-window power"},
            "failed": failed if failed is not None else ["tracking_error_daily_pp"],
            "verdict": verdict,
            "thresholds": {"tracking_error_daily_pp_max": 0.2},
        },
        "metrics": {},
        "provenance": {"data_as_of": "2026-09-09", "code_commit": "a" * 40},
    }


def _paired(verdict: str = "hold") -> dict:
    return {
        "paired": {
            "rule_id": "atr_1p0_25_40", "n_days": 1, "ready": False, "window_days": 120,
            "t_min": 1.5, "diff_gt": 0.0, "verdict": verdict, "switch": verdict == "switch",
            "corr": None, "mean_diff_pp": None, "t_stat": None,
            "candidate_params": {"pb_atr_mult": 1.0, "pb_stop_lo": 0.025, "pb_stop_hi": 0.04},
        },
        "provenance": {"data_as_of": "2026-09-09"},
    }


def _prereg(rule_id: str, frozen_at: str) -> dict:
    return {"rule_id": rule_id, "version": 1, "frozen_at": frozen_at,
            "record_sha256": "b" * 64, "trials": {"family": "d_forward_risk_gate"}}


class ForwardReadersTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "outputs" / "forward" / "prereg").mkdir(parents=True, exist_ok=True)
        patcher = mock.patch.object(qm, "_project_root", lambda: str(self.root))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write(self, rel: str, payload) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload if isinstance(payload, str) else json.dumps(payload),
                        encoding="utf-8")
        return path

    def test_missing_artifacts_read_as_none(self):
        self.assertIsNone(qm.read_forward_health())
        self.assertIsNone(qm.read_forward_paired())
        self.assertEqual(qm.read_forward_prereg(), [])
        bundle = qm.read_forward()
        self.assertIsNone(bundle["gate_verdict"])
        self.assertIsNone(bundle["candidate_verdict"])

    def test_bundle_reports_gate_and_candidate_verdicts(self):
        self._write("outputs/forward/forward_health.json", _health("fail"))
        self._write("outputs/forward/paired_atr_1p0_25_40.json", _paired("hold"))
        bundle = qm.read_forward()
        self.assertEqual(bundle["gate_verdict"], "fail")
        self.assertEqual(bundle["candidate_verdict"], "hold")
        self.assertEqual(bundle["health"]["artifact"], "outputs/forward/forward_health.json")
        self.assertIn("artifact", bundle["paired"])

    def test_newest_health_artifact_wins(self):
        old = self._write("outputs/forward/forward_health_old.json", _health("pass"))
        new = self._write("outputs/forward/forward_health.json", _health("fail"))
        # make the ordering explicit instead of relying on mtime resolution
        os.utime(old, (time.time() - 600, time.time() - 600))
        os.utime(new, (time.time(), time.time()))
        self.assertEqual(qm.read_forward()["gate_verdict"], "fail")
        os.utime(new, (time.time() - 600, time.time() - 600))
        os.utime(old, (time.time(), time.time()))
        self.assertEqual(qm.read_forward()["gate_verdict"], "pass")

    def test_corrupt_artifact_raises(self):
        self._write("outputs/forward/forward_health.json", "{not json")
        with self.assertRaises(qm.OutputCorruptError):
            qm.read_forward_health()

    def test_prereg_records_are_newest_first(self):
        self._write("outputs/forward/prereg/prereg_a_v1.json",
                    _prereg("d_forward_2026h2", "2026-09-09T23:04:47"))
        self._write("outputs/forward/prereg/prereg_b_v1.json",
                    _prereg("d_forward_atr_candidate_2026h2", "2026-09-09T23:04:48"))
        recs = qm.read_forward_prereg()
        self.assertEqual([r["rule_id"] for r in recs],
                         ["d_forward_atr_candidate_2026h2", "d_forward_2026h2"])
        self.assertTrue(all("artifact" in r for r in recs))

    def test_corrupt_prereg_is_listed_not_fatal(self):
        self._write("outputs/forward/prereg/prereg_broken_v1.json", "{nope")
        recs = qm.read_forward_prereg()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["error"], "unreadable")


class ForwardRouterTest(unittest.TestCase):
    def test_router_returns_the_bundle(self):
        with mock.patch.object(qm, "read_forward", lambda: {"gate_verdict": "pass"}):
            self.assertEqual(qr.forward_state(), {"gate_verdict": "pass"})

    def test_router_maps_corrupt_output_to_502(self):
        def _boom():
            raise qm.OutputCorruptError("broken")
        with mock.patch.object(qm, "read_forward", _boom):
            with self.assertRaises(HTTPException) as ctx:
                qr.forward_state()
        self.assertEqual(ctx.exception.status_code, 502)
