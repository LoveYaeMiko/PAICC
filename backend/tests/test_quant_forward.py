"""Tests for the forward-period panel feed (``/quant/forward``).

Covers the business readers against a temporary FQA-root stub — the canonical
``forward_health.json`` / ``paired_atr_1p0_25_40.json`` artifact wins over a
newer shakedown copy (FQA labels those "not the forward window, pipeline
shakedown only"), a corrupt candidate is skipped and listed instead of blanking
the whole bundle, pre-registration records are listed newest-first, and the
router maps a missing/valid payload as documented.

Also covers the scheduled/manual gate job ``run_forward_health``: FQA's
``scripts/forward_health.py`` exits 1 when a hard gate FAILED, which is a valid
gate RESULT but a FAILED job (never 成功).
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
from app.services import quant_scheduler as qs


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
        self.assertFalse(bundle["health"]["fallback"])
        self.assertIn("artifact", bundle["paired"])

    def test_canonical_health_artifact_beats_a_newer_shakedown_copy(self):
        canonical = self._write("outputs/forward/forward_health.json", _health("fail"))
        shakedown = self._write(
            "outputs/forward/forward_health_shakedown_20260909.json", _health("pass")
        )
        # The shakedown copy is the NEWEST file — FQA labels it "pipeline
        # shakedown only", so the canonical forward window must still win.
        os.utime(canonical, (time.time() - 600, time.time() - 600))
        os.utime(shakedown, (time.time(), time.time()))

        health = qm.read_forward_health()
        self.assertEqual(health["gate"]["verdict"], "fail")
        self.assertEqual(health["artifact"], "outputs/forward/forward_health.json")
        self.assertFalse(health["fallback"])
        self.assertEqual(health["corrupt"], [])

    def test_missing_canonical_falls_back_and_flags_it(self):
        old = self._write(
            "outputs/forward/forward_health_shakedown_20260901.json", _health("pass")
        )
        new = self._write(
            "outputs/forward/forward_health_shakedown_20260909.json", _health("fail")
        )
        os.utime(old, (time.time() - 600, time.time() - 600))
        os.utime(new, (time.time(), time.time()))

        health = qm.read_forward_health()
        self.assertTrue(health["fallback"])
        self.assertEqual(
            health["artifact"], "outputs/forward/forward_health_shakedown_20260909.json"
        )
        self.assertEqual(qm.read_forward()["gate_verdict"], "fail")

    def test_paired_prefers_the_canonical_path(self):
        canonical = self._write("outputs/forward/paired_atr_1p0_25_40.json", _paired("hold"))
        other = self._write("outputs/forward/paired_atr_0p5_20_40.json", _paired("switch"))
        os.utime(canonical, (time.time() - 600, time.time() - 600))
        os.utime(other, (time.time(), time.time()))

        paired = qm.read_forward_paired()
        self.assertEqual(paired["paired"]["verdict"], "hold")
        self.assertFalse(paired["fallback"])
        self.assertEqual(paired["corrupt"], [])
        self.assertEqual(qm.read_forward()["candidate_verdict"], "hold")

    def test_missing_canonical_paired_falls_back_and_flags_it(self):
        self._write("outputs/forward/paired_atr_0p5_20_40.json", _paired("switch"))
        paired = qm.read_forward_paired()
        self.assertTrue(paired["fallback"])
        self.assertEqual(paired["artifact"], "outputs/forward/paired_atr_0p5_20_40.json")

    def test_corrupt_candidate_is_skipped_not_fatal(self):
        broken = self._write(
            "outputs/forward/forward_health_shakedown_20260909.json", "{not json"
        )
        good = self._write(
            "outputs/forward/forward_health_shakedown_20260901.json", _health("pass")
        )
        # the corrupt file is the newest: before the fix it raised and the whole
        # bundle (gate + candidate + prereg) disappeared behind a 502.
        os.utime(good, (time.time() - 600, time.time() - 600))
        os.utime(broken, (time.time(), time.time()))

        health = qm.read_forward_health()
        self.assertEqual(health["gate"]["verdict"], "pass")
        self.assertTrue(health["fallback"])
        self.assertEqual(
            health["corrupt"], ["outputs/forward/forward_health_shakedown_20260909.json"]
        )
        # the bundle keeps working and still surfaces the skipped file
        bundle = qm.read_forward()
        self.assertEqual(bundle["gate_verdict"], "pass")
        self.assertEqual(bundle["health"]["corrupt"], health["corrupt"])

    def test_corrupt_canonical_falls_back_and_records_it(self):
        self._write("outputs/forward/forward_health.json", "{not json")
        self._write("outputs/forward/forward_health_shakedown_20260909.json", _health("pass"))

        health = qm.read_forward_health()
        self.assertEqual(health["gate"]["verdict"], "pass")
        self.assertTrue(health["fallback"])
        self.assertEqual(health["corrupt"], ["outputs/forward/forward_health.json"])

    def test_corrupt_artifact_raises_when_every_candidate_is_corrupt(self):
        self._write("outputs/forward/forward_health.json", "{not json")
        self._write("outputs/forward/forward_health_shakedown_20260909.json", "{also broken")
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


class ForwardHealthJobTest(unittest.TestCase):
    """``run_forward_health`` — the scheduled job + ``POST /quant/forward/run``.

    FQA's ``scripts/forward_health.py`` exits 1 when a hard gate FAILED. That is
    a valid gate *result*, but the JOB failed: reporting it as ``ok`` (the old
    ``rc in (0, 1)``) stored a failing gate as 成功, so nobody was ever alerted.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "outputs" / "forward").mkdir(parents=True, exist_ok=True)
        self._settings: dict[str, str] = {
            "quant_root": str(self.root),
            "quant_forward_health_enabled": "true",
        }
        self._patch_settings()
        # the PIT-DB pre-flight must not touch Docker in a unit test
        pit = mock.patch.object(qs, "_ensure_pit_db_or_fail", return_value=None)
        pit.start()
        self.addCleanup(pit.stop)
        log = mock.patch.object(qs.db, "log_operation")
        self.log = log.start()
        self.addCleanup(log.stop)

    def _patch_settings(self):
        def _get(key, default=None):
            return self._settings.get(key, default)

        patcher = mock.patch.object(qs.settings, "get", side_effect=_get)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write_artifact(self, verdict: str, failed: list[str]) -> None:
        payload = _health(verdict, failed)
        (self.root / "outputs" / "forward" / "forward_health.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )

    def _run(self, returncode: int):
        proc = {
            "command": "python scripts/forward_health.py",
            "returncode": returncode,
            "stdout": "gate evaluated",
            "stderr": "",
        }
        with mock.patch.object(qs.quant_manager, "run_project_command", return_value=proc):
            return qs.run_forward_health()

    def test_failed_gate_is_a_failed_job(self):
        self._write_artifact("fail", ["tracking_error_daily_pp", "availability"])
        result = self._run(1)

        self.assertFalse(result["ok"])
        self.assertEqual(result["returncode"], 1)
        self.assertEqual(result["verdict"], "fail")
        self.assertEqual(result["failed"], ["tracking_error_daily_pp", "availability"])
        # ... and the operation log carries the same verdict/failed list, so the
        # failure is visible without opening the artifact.
        action, _params, log_result = self.log.call_args.args
        self.assertEqual(action, "quant_forward_health")
        self.assertFalse(log_result["ok"])
        self.assertEqual(log_result["verdict"], "fail")
        self.assertEqual(log_result["failed"], ["tracking_error_daily_pp", "availability"])

    def test_passing_gate_is_a_successful_job(self):
        self._write_artifact("pass", [])
        result = self._run(0)

        self.assertTrue(result["ok"])
        self.assertEqual(result["verdict"], "pass")
        self.assertEqual(result["failed"], [])
        self.assertNotIn("artifact_error", result)

    def test_missing_artifact_does_not_fail_the_job(self):
        # exit 0 with no artifact on disk: ok stays keyed on the exit code, and
        # the missing verdict is reported instead of raising.
        result = self._run(0)

        self.assertTrue(result["ok"])
        self.assertIsNone(result["verdict"])
        self.assertEqual(result["failed"], [])

    def test_corrupt_artifact_does_not_crash_the_job(self):
        (self.root / "outputs" / "forward" / "forward_health.json").write_text(
            "{not json", encoding="utf-8"
        )
        result = self._run(1)

        self.assertFalse(result["ok"])
        self.assertIn("OutputCorruptError", str(result.get("artifact_error")))

    def test_disabled_setting_skips_the_job(self):
        self._settings["quant_forward_health_enabled"] = "false"
        with mock.patch.object(qs.quant_manager, "run_project_command") as runner:
            result = qs.run_forward_health()

        runner.assert_not_called()
        self.assertTrue(result["ok"])
        self.assertIn("skipped", result)


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

    def test_router_starts_the_forward_health_task(self):
        """``POST /quant/forward/run`` is confirmation-gated and returns a task id."""
        with mock.patch.object(qr, "require_confirmation") as guard, \
             mock.patch.object(qr.task_manager, "start_task", return_value="tid") as start:
            out = qr.run_forward(qr.RunShadowRequest(confirmation_id="cid"))

        guard.assert_called_once_with("cid", action="run_quant_forward")
        start.assert_called_once_with("quant_forward_health", qs.run_forward_health)
        self.assertEqual(out, {"task_id": "tid", "status": "started"})
