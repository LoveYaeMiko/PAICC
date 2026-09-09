"""Tests for ``quant_scheduler.get_status()['jobs']`` (panel 「任务调度」 card).

Two contracts are covered:

* the static catalog (``_job_catalog``) is complete and pure — 8 jobs, full
  field set, ``next_run=None`` — so the card renders even when the in-process
  APScheduler is not running;
* a live scheduler's ``get_jobs()`` overrides the cron label / ``next_run``,
  while ``last_run`` / ``last_status`` come from the in-memory run records and
  stay ``None`` (never invented) when no record exists.
"""

from __future__ import annotations

import unittest
from datetime import datetime
from unittest import mock

from apscheduler.triggers.cron import CronTrigger

from app.services import quant_scheduler as qs

#: id -> planned cron label (the static table's plan times).
EXPECTED_CRON = {
    "quant_live_start": "mon-fri 09:25",
    "quant_depth_snapshot": "mon-fri 14:40",  # moved off 14:50 to clear preclose
    "quant_preclose": "mon-fri 14:50",
    "quant_intraday_refresh": "mon-fri 15:02",
    "quant_shadow_daily": "mon-fri 15:10",
    "quant_d_challenger": "mon-fri 17:45",
    "quant_forward_candidate": "mon-fri 15:20",
    "quant_live_watchdog": "every 5m",
    "quant_forward_health": "sat 18:30",
    "quant_calibrate": "sat 18:00",
    "quant_weekly_cycle": "sun 18:00",
}

REQUIRED_KEYS = {"id", "name", "cron", "next_run", "last_run", "last_status"}

_SETTING_DEFAULTS = {
    "quant_shadow_daily_time": "15:10",
    "quant_calibrate_time": "18:00",
    "quant_weekly_time": "18:00",
    "quant_forward_health_time": "18:30",
    "quant_shadow_auto_email": "true",
    "quant_autopilot_enabled": "true",
}


def _fake_get(key, default=None):
    """Settings stub — keeps the tests off the real ``paicc.db``."""
    return _SETTING_DEFAULTS.get(key, default)


class _FakeJob:
    def __init__(self, job_id: str, trigger=None, next_run_time=None):
        self.id = job_id
        self.trigger = trigger
        self.next_run_time = next_run_time


class _FakeScheduler:
    def __init__(self, jobs):
        self._jobs = list(jobs)

    def get_jobs(self):
        return list(self._jobs)


class _BrokenScheduler:
    def get_jobs(self):
        raise RuntimeError("scheduler exploded")


class JobCatalogTest(unittest.TestCase):
    def test_static_catalog_is_complete_and_pure(self):
        jobs = qs._job_catalog()
        self.assertEqual(len(jobs), 11)
        self.assertEqual([job["id"] for job in jobs], list(EXPECTED_CRON))
        for job in jobs:
            self.assertEqual(set(job), REQUIRED_KEYS)
            self.assertEqual(job["cron"], EXPECTED_CRON[job["id"]])
            self.assertTrue(job["name"])
            self.assertIsNone(job["next_run"])
            self.assertIsNone(job["last_run"])
            self.assertIsNone(job["last_status"])

    def test_catalog_returns_fresh_objects(self):
        first = qs._job_catalog()
        first[0]["cron"] = "mutated"
        first.append({"id": "junk"})
        second = qs._job_catalog()
        self.assertEqual(second[0]["cron"], EXPECTED_CRON["quant_live_start"])
        self.assertEqual(len(second), 11)


class StatusWithoutSchedulerTest(unittest.TestCase):
    """``jobs`` must not degrade to an empty list when the scheduler is down."""

    def setUp(self):
        patcher = mock.patch.object(qs, "_scheduler", None)
        patcher.start()
        self.addCleanup(patcher.stop)
        settings_patcher = mock.patch.object(qs.settings, "get", side_effect=_fake_get)
        settings_patcher.start()
        self.addCleanup(settings_patcher.stop)
        started_patcher = mock.patch.object(qs, "_started", False)
        started_patcher.start()
        self.addCleanup(started_patcher.stop)

    def test_jobs_still_listed_with_static_times(self):
        status = qs.get_status()
        self.assertFalse(status["scheduler_running"])
        jobs = status["jobs"]
        self.assertEqual(len(jobs), 11)
        for job in jobs:
            self.assertEqual(set(job), REQUIRED_KEYS)
            self.assertEqual(job["cron"], EXPECTED_CRON[job["id"]])
            self.assertIsNone(job["next_run"])
            self.assertIsNone(job["last_run"])
            self.assertIsNone(job["last_status"])

    def test_backward_compatible_fields_kept(self):
        status = qs.get_status()
        for key in (
            "shadow_daily_time",
            "calibrate_time",
            "weekly_time",
            "shadow_auto_email",
            "autopilot_enabled",
            "scheduler_running",
            "last_shadow_run",
            "last_calibration_run",
            "last_autopilot_run",
            "last_weekly_run",
        ):
            self.assertIn(key, status)


class StatusWithLiveSchedulerTest(unittest.TestCase):
    """Live ``get_jobs()`` values override the static plan times."""

    def setUp(self):
        settings_patcher = mock.patch.object(qs.settings, "get", side_effect=_fake_get)
        settings_patcher.start()
        self.addCleanup(settings_patcher.stop)
        for name in ("_last_shadow_run", "_last_calibration_run",
                     "_last_autopilot_run", "_last_weekly_run"):
            patcher = mock.patch.object(qs, name, None)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _install(self, scheduler, started=True):
        patcher = mock.patch.object(qs, "_scheduler", scheduler)
        patcher.start()
        self.addCleanup(patcher.stop)
        started_patcher = mock.patch.object(qs, "_started", started)
        started_patcher.start()
        self.addCleanup(started_patcher.stop)

    def test_real_trigger_and_next_run_override_static(self):
        jobs = [
            _FakeJob(
                "quant_live_start",
                CronTrigger(day_of_week="mon-fri", hour=9, minute=25),
                datetime(2026, 9, 9, 9, 25),
            ),
            # Deliberately different from the static label (09:25) to prove the
            # live trigger wins.
            _FakeJob(
                "quant_shadow_daily",
                CronTrigger(day_of_week="mon-fri", hour=16, minute=5),
                datetime(2026, 9, 9, 16, 5),
            ),
            _FakeJob("quant_not_in_catalog", CronTrigger(hour=1), datetime(2026, 9, 9, 1, 0)),
        ]
        self._install(_FakeScheduler(jobs))

        status = qs.get_status()
        self.assertTrue(status["scheduler_running"])
        by_id = {job["id"]: job for job in status["jobs"]}
        self.assertEqual(len(status["jobs"]), 11)
        self.assertEqual(by_id["quant_live_start"]["cron"], "mon-fri 09:25")
        self.assertEqual(by_id["quant_live_start"]["next_run"], "2026-09-09T09:25:00")
        self.assertEqual(by_id["quant_shadow_daily"]["cron"], "mon-fri 16:05")
        self.assertEqual(by_id["quant_shadow_daily"]["next_run"], "2026-09-09T16:05:00")
        # Jobs the scheduler does not report keep their static plan time.
        self.assertEqual(by_id["quant_calibrate"]["cron"], "sat 18:00")
        self.assertIsNone(by_id["quant_calibrate"]["next_run"])
        # Unknown job ids are ignored (the catalog defines the card).
        self.assertNotIn("quant_not_in_catalog", by_id)

    def test_timezone_aware_next_run_is_rendered_as_local_wall_clock(self):
        from datetime import timezone, timedelta

        tz = timezone(timedelta(hours=8))
        self._install(
            _FakeScheduler(
                [_FakeJob("quant_calibrate", CronTrigger(day_of_week="sat", hour=18, minute=0),
                          datetime(2026, 9, 12, 18, 0, tzinfo=tz))]
            )
        )
        by_id = {job["id"]: job for job in qs.get_status()["jobs"]}
        self.assertEqual(by_id["quant_calibrate"]["next_run"], "2026-09-12T18:00:00")

    def test_broken_scheduler_falls_back_to_static_table(self):
        self._install(_BrokenScheduler())
        jobs = qs.get_status()["jobs"]
        self.assertEqual(len(jobs), 11)
        for job in jobs:
            self.assertEqual(job["cron"], EXPECTED_CRON[job["id"]])
            self.assertIsNone(job["next_run"])

    def test_last_run_and_status_from_run_records(self):
        shadow = {"ok": True, "ts": "2026-09-09T15:11:00"}
        autopilot = {"ok": True, "ts": "2026-09-09T16:00:00"}
        calibration = {"ok": False, "returncode": 1, "ts": "2026-09-12T18:02:00"}
        weekly = {"returncode": 0, "ts": "2026-09-13T18:05:00"}
        with mock.patch.object(qs, "_last_shadow_run", shadow), \
             mock.patch.object(qs, "_last_autopilot_run", autopilot), \
             mock.patch.object(qs, "_last_calibration_run", calibration), \
             mock.patch.object(qs, "_last_weekly_run", weekly):
            self._install(_FakeScheduler([]))
            by_id = {job["id"]: job for job in qs.get_status()["jobs"]}

        # The daily slot runs autopilot or shadow -> the newest record wins.
        self.assertEqual(by_id["quant_shadow_daily"]["last_run"], "2026-09-09T16:00:00")
        self.assertEqual(by_id["quant_shadow_daily"]["last_status"], "ok")
        self.assertEqual(by_id["quant_calibrate"]["last_status"], "failed")
        self.assertEqual(by_id["quant_weekly_cycle"]["last_status"], "ok")
        # Jobs with no in-memory record stay None (the panel shows「—」).
        for job_id in ("quant_live_start", "quant_depth_snapshot", "quant_preclose",
                       "quant_intraday_refresh", "quant_d_challenger"):
            self.assertIsNone(by_id[job_id]["last_run"])
            self.assertIsNone(by_id[job_id]["last_status"])

    def test_run_status_prefers_ok_over_returncode(self):
        self.assertEqual(qs._run_status({"ok": True, "returncode": 1}), "ok")
        self.assertEqual(qs._run_status({"ok": False, "returncode": 0}), "failed")
        self.assertEqual(qs._run_status({"returncode": 0}), "ok")
        self.assertEqual(qs._run_status({"returncode": 2}), "failed")

    def test_run_status_reports_skipped(self):
        # A gated-off job returns ok=True + skipped=...; it must NOT read 成功.
        self.assertEqual(
            qs._run_status({"ok": True, "skipped": "D 轨模型自优化循环未启用"}), "skipped"
        )
        self.assertEqual(qs._run_status({"ok": True}), "ok")
        self.assertIsNone(qs._run_status({}))
        self.assertIsNone(qs._run_status(None))


class SingleTrackDailyEmailTest(unittest.TestCase):
    """The daily email must read as one D track — no multi-track/empty titles."""

    def test_shadow_daily_email_subject_and_body_are_single_track(self):
        accounts = {
            "D_5W": {
                "status": {"last_trading_date": "2026-09-08", "equity": {"latest": 50123.0}},
                "report": "# D 轨影子日报\n\n- 净值: 50,123.00",
            }
        }
        proc = {"command": "python cli.py shadow", "returncode": 0, "stdout": "", "stderr": ""}
        with mock.patch.object(qs.settings, "get", side_effect=_fake_get), \
             mock.patch.object(qs, "_ensure_pit_db_or_fail", return_value=None), \
             mock.patch.object(qs.quant_manager, "run_project_command", return_value=proc), \
             mock.patch.object(qs.quant_manager, "read_shadow_accounts", return_value=accounts), \
             mock.patch.object(qs, "_generate_shadow_commentary", return_value=""), \
             mock.patch.object(qs.mailer, "send_mail", return_value={"ok": True}) as fake_mail, \
             mock.patch.object(qs.db, "log_operation"), \
             mock.patch.object(qs.ws, "publish"), \
             mock.patch.object(qs, "_last_shadow_run", None):
            result = qs.run_shadow_daily()

        subject, body = fake_mail.call_args.args[0], fake_mail.call_args.args[1]
        self.assertEqual(subject, "FQA D 轨影子日报 — 2026-09-08")
        self.assertIn("## 账户 D_5W", body)
        for retired in ("A_200W", "B_10W", "C_5W", "双资金轨", "多资金轨"):
            self.assertNotIn(retired, body)
        self.assertEqual(result["accounts"], ["D_5W"])
        self.assertTrue(result["ok"])
        # ``_stamp`` gives the record a timestamp so the panel can show last_run.
        self.assertTrue(result["ts"])


if __name__ == "__main__":
    unittest.main()
