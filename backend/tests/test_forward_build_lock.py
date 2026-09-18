"""The two market-building forward jobs must never run at the same time.

2026-09-18: the 15:40 gate was still replaying when the 15:20-scheduled candidate
actually got going, so two ~10 GB market builds (plus the 15:45 daily loop) thrashed
the machine — the candidate exited non-zero, the gate hung until the backend was
restarted hours later, and a whole day of forward artifacts was lost. The catch-up
path already ran these jobs sequentially; the cron path did not.
"""
from __future__ import annotations

import threading
import time
import unittest
from unittest import mock

from app.services import quant_scheduler as qs


class ForwardBuildLockTest(unittest.TestCase):
    def setUp(self):
        # The lock is process-global and the job bodies are heavy: never let a test
        # WAIT on it (a 30-minute block would look like a hung suite) and never let
        # a test leave it held (later tests would then skip their work).
        self._wait = qs._FORWARD_BUILD_WAIT
        qs._FORWARD_BUILD_WAIT = 0
        self.addCleanup(self._restore)

    def _restore(self):
        qs._FORWARD_BUILD_WAIT = self._wait
        if qs._FORWARD_BUILD_LOCK.locked():
            qs._FORWARD_BUILD_LOCK.release()

    def test_busy_lock_skips_instead_of_starting_a_second_build(self):
        qs._FORWARD_BUILD_LOCK.acquire()
        with mock.patch.object(qs, "_ensure_pit_db_or_fail", return_value=None), \
             mock.patch.object(qs.quant_manager, "run_project_command") as runner, \
             mock.patch.object(qs.db, "log_operation") as log:
            result = qs.run_forward_health()

        runner.assert_not_called()                 # no second market build
        self.assertFalse(result["ok"])
        self.assertIn("另一前向任务", result["skipped"])
        self.assertEqual(log.call_args.args[0], "quant_forward_health")

    def test_candidate_also_skips_when_busy(self):
        qs._FORWARD_BUILD_LOCK.acquire()
        with mock.patch.object(qs, "_ensure_pit_db_or_fail", return_value=None), \
             mock.patch.object(qs.quant_manager, "run_project_command") as runner, \
             mock.patch.object(qs.db, "log_operation"):
            result = qs.run_forward_candidate_daily()

        runner.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertIn("quant_forward_candidate", result["skipped"])

    def test_the_lock_is_released_after_a_run(self):
        proc = {"returncode": 0, "stdout": "ok", "stderr": ""}
        with mock.patch.object(qs, "_ensure_pit_db_or_fail", return_value=None), \
             mock.patch.object(qs.quant_manager, "run_project_command", return_value=proc), \
             mock.patch.object(qs.quant_manager, "read_forward_health",
                               lambda: {"gate": {"verdict": "pass", "failed": []}}), \
             mock.patch.object(qs.db, "log_operation"):
            qs.run_forward_health()
        self.assertFalse(qs._FORWARD_BUILD_LOCK.locked())

    def test_serialization_is_real(self):
        """Two threads entering the decorated body must not overlap."""
        events: list[str] = []
        gate = threading.Event()

        @qs._serialized_forward_build("probe_job")
        def _body():
            events.append("enter")
            gate.wait(5)
            events.append("exit")
            return {"ok": True}

        first = threading.Thread(target=_body)
        first.start()
        time.sleep(0.2)                      # let the first one take the lock
        try:
            with mock.patch.object(qs.db, "log_operation"):
                second = _body()
        finally:
            gate.set()
            first.join(5)

        self.assertEqual(events, ["enter", "exit"])      # never interleaved
        self.assertFalse(second["ok"])
        self.assertIn("probe_job", second["skipped"])

    def test_still_registered_as_cron_jobs(self):
        """The decorator must not break the scheduler registration contract.

        Both jobs stay zero-arg and return a dict — asserted WITHOUT calling the
        real body: that would spawn ``scripts/forward_health.py`` for real (a ~15 GB
        market build), which is exactly what happened the first time this test was
        written (2026-09-18) and it wedged the machine for five minutes.
        """
        for fn in (qs.run_forward_health, qs.run_forward_candidate_daily):
            self.assertTrue(callable(fn))
        # the wrapped body is what runs when the lock is free; patch the inner job
        with mock.patch.object(qs, "_ensure_pit_db_or_fail", return_value={"ok": False, "stage": "test"}), \
             mock.patch.object(qs.quant_manager, "run_project_command") as runner, \
             mock.patch.object(qs.db, "log_operation"):
            out = qs.run_forward_health()
        self.assertIsInstance(out, dict)
        runner.assert_not_called()          # the PIT pre-flight failed → no CLI run


if __name__ == "__main__":
    unittest.main()
