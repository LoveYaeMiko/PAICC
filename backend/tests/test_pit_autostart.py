"""Boot-time PIT/Docker autostart (2026-09-11: Docker was simply not running).

The backend used to come up "ready" with its data layer down: the panel answered,
and the first job of the morning (09:25 live trader, or 15:10 shadow) discovered
the problem. Now the backend starts the stack itself at boot — but it must do so
WITHOUT blocking the FastAPI lifespan, because ``ensure_pit_db_up`` legitimately
waits minutes for a cold Docker daemon.
"""
from __future__ import annotations

import unittest
from unittest import mock

from app.services import quant_manager as qm


class PitAutostartTest(unittest.TestCase):
    def setUp(self):
        # the latch is process-global: reset it so each test starts clean
        self._saved = qm._pit_autostart_started
        qm._pit_autostart_started = False
        self.addCleanup(self._restore)

    def _restore(self):
        # a spawned worker must not outlive the test: it would call the REAL
        # ensure_pit_db_up after the mocks are gone and race the next test's
        # assertions (observed: an intermittent suite failure on 2026-09-13).
        import threading

        for t in threading.enumerate():
            if t.name == "pit-autostart" and t.is_alive():
                t.join(timeout=5)
        qm._pit_autostart_started = self._saved

    def _settings(self, enabled: bool):
        patcher = mock.patch.object(
            qm.settings, "get_bool", lambda key, default=True: enabled
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_startup_hook_returns_immediately_while_the_worker_waits(self):
        """The hook must not run the (potentially minutes-long) wait on the caller.

        The worker is parked on an event, so the assertion is deterministic: if the
        hook had done the Docker work inline, ``ensure_pit_db_on_startup`` could not
        have returned before the event was released.
        """
        import threading
        import time

        self._settings(True)
        entered = threading.Event()
        release = threading.Event()

        def _slow():
            entered.set()
            release.wait(5)
            return {"ok": True, "stage": "healthy", "detail": "x"}

        with mock.patch.object(qm, "ensure_pit_db_up", _slow), \
             mock.patch.object(qm, "_PIT_AUTOSTART_RETRY_DELAY", 0):
            t0 = time.time()
            out = qm.ensure_pit_db_on_startup()
            elapsed = time.time() - t0

        self.assertTrue(out["started"])
        self.assertLess(elapsed, 1.0, "the boot hook blocked the FastAPI lifespan")
        self.assertTrue(entered.wait(5), "the worker thread never started")
        release.set()

    def test_worker_logs_the_outcome(self):
        with mock.patch.object(qm, "ensure_pit_db_up",
                               lambda: {"ok": False, "stage": "docker_daemon",
                                        "detail": "守护进程未就绪"}), \
             mock.patch.object(qm, "_PIT_AUTOSTART_DEADLINE", 0), \
             mock.patch.object(qm.db, "log_operation") as log:
            qm._pit_autostart_worker()

        action, params, result = log.call_args.args
        self.assertEqual(action, "quant_pit_autostart")
        self.assertEqual(params["trigger"], "backend_startup")
        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "docker_daemon")
        self.assertEqual(result["attempts"], 1)

    def test_worker_retries_until_healthy(self):
        """One failed attempt must not end the boot hook (2026-09-13 flapping engine)."""
        calls: list[int] = []
        results = [{"ok": False, "stage": "health", "detail": "engine flapping"},
                   {"ok": False, "stage": "health", "detail": "still flapping"},
                   {"ok": True, "stage": "healthy", "detail": "PIT 数据库就绪"}]

        def _up():
            calls.append(1)
            return results[min(len(calls) - 1, len(results) - 1)]

        with mock.patch.object(qm, "ensure_pit_db_up", _up), \
             mock.patch.object(qm, "_PIT_AUTOSTART_RETRY_DELAY", 0), \
             mock.patch.object(qm.db, "log_operation") as log:
            qm._pit_autostart_worker()

        self.assertEqual(len(calls), 3)
        self.assertTrue(log.call_args.args[2]["ok"])
        self.assertEqual(log.call_args.args[2]["attempts"], 3)

    def test_worker_gives_up_at_the_deadline(self):
        with mock.patch.object(qm, "ensure_pit_db_up",
                               lambda: {"ok": False, "stage": "docker_daemon",
                                        "detail": "no docker"}), \
             mock.patch.object(qm, "_PIT_AUTOSTART_DEADLINE", 0), \
             mock.patch.object(qm.db, "log_operation") as log:
            qm._pit_autostart_worker()

        self.assertFalse(log.call_args.args[2]["ok"])
        self.assertEqual(log.call_args.args[2]["attempts"], 1)

    def test_worker_survives_an_exception(self):
        def _boom():
            raise RuntimeError("docker missing")

        with mock.patch.object(qm, "ensure_pit_db_up", _boom), \
             mock.patch.object(qm, "_PIT_AUTOSTART_DEADLINE", 0), \
             mock.patch.object(qm.db, "log_operation") as log:
            qm._pit_autostart_worker()          # must not raise

        self.assertFalse(log.call_args.args[2]["ok"])
        self.assertEqual(log.call_args.args[2]["stage"], "exception")

    def test_disabled_setting_skips_it(self):
        self._settings(False)
        with mock.patch.object(qm, "ensure_pit_db_up") as up:
            out = qm.ensure_pit_db_on_startup()
        up.assert_not_called()
        self.assertFalse(out["started"])
        self.assertIn("quant_pit_autostart=false", out["skipped"])

    def test_second_call_is_a_noop(self):
        self._settings(True)
        # the worker body is patched out: this test is about the latch, and a real
        # worker could outlive the mock and touch Docker/DB in the background
        with mock.patch.object(qm, "_pit_autostart_worker") as worker:
            first = qm.ensure_pit_db_on_startup()
            second = qm.ensure_pit_db_on_startup()
        self.assertTrue(first["started"])
        self.assertFalse(second["started"])
        worker.assert_called_once()

    def test_registered_as_a_background_service(self):
        """The hook is useless if nothing calls it at boot."""
        from app import main

        pairs = [tuple(x) for x in main.BACKGROUND_SERVICES]
        self.assertIn(("app.services.quant_manager", "ensure_pit_db_on_startup"), pairs)
        # ... and it must be the non-blocking variant, not ensure_pit_db_up
        self.assertNotIn(("app.services.quant_manager", "ensure_pit_db_up"), pairs)


if __name__ == "__main__":
    unittest.main()
