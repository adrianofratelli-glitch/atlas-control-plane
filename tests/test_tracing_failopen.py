"""Langfuse fail-open without log noise and without blocking a turn.

One warning per outage, then silence + counters; the probe and auth_check run in a
background thread, and clients that are dropped are shut down (no leaked threads/pools).

Seen live on 2026-11-12/13: with the self-host down, the SDK printed
"Unexpected error occurred..." (logger `langfuse`) and the turns ran untraced.
These tests pin the contract of backend/tracing.py without any network.
"""
import logging
import os
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend import observability, tracing  # noqa: E402

ENV = {"LANGFUSE_PUBLIC_KEY": "pk-test", "LANGFUSE_SECRET_KEY": "sk-test", "LANGFUSE_HOST": "http://127.0.0.1:9"}


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


class FakeLangfuse:
    auth_ok = True
    built = 0
    instances = []

    def __init__(self):
        FakeLangfuse.built += 1
        self.shutdowns = 0
        FakeLangfuse.instances.append(self)

    def auth_check(self):
        if not FakeLangfuse.auth_ok:
            # what the real SDK does on failure: logs through the `langfuse` logger
            logging.getLogger("langfuse").error("Unexpected error occurred. Please check your request.")
            return False
        return True

    def trace(self, **kwargs):
        return SimpleNamespace(**kwargs)

    def shutdown(self):
        self.shutdowns += 1


def _wait(predicate, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.005)
    return False


class TracingFailOpenTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNone(tracing._connect_thread, "nothing may start a Langfuse connection outside these tests")
        self.saved = (tracing._client, tracing._retry_at, dict(tracing._state))
        tracing._client, tracing._retry_at = None, 0.0
        tracing._state.update(status="pending", reason=None, warned=False)
        self.saved_counters = dict(observability.metrics.counters)
        observability.metrics.counters.clear()
        FakeLangfuse.auth_ok, FakeLangfuse.built, FakeLangfuse.instances = True, 0, []
        self.capture = _Capture()
        logging.getLogger().addHandler(self.capture)
        self.saved_level = tracing.logger.level
        tracing.logger.setLevel(logging.DEBUG)
        self.env = patch.dict(os.environ, ENV)
        self.env.start()
        self.fake_module = patch.dict(sys.modules, {"langfuse": SimpleNamespace(Langfuse=FakeLangfuse)})
        self.fake_module.start()

    def tearDown(self):
        if tracing._connect_thread is not None:
            tracing._connect_thread.join(timeout=5)
        tracing._connect_thread = None
        self.fake_module.stop()
        self.env.stop()
        logging.getLogger().removeHandler(self.capture)
        tracing.logger.setLevel(self.saved_level)
        tracing._client, tracing._retry_at = self.saved[0], self.saved[1]
        tracing._state.clear()
        tracing._state.update(self.saved[2])
        observability.metrics.counters.clear()
        observability.metrics.counters.update(self.saved_counters)

    def _turn(self):
        return tracing.start_trace(name="torre.turn", user_id=None, session_id="s", input_text="oi")

    def _connect_now(self):
        """What the background thread does, run synchronously."""
        thread = tracing.warm_up()
        if thread is not None:
            thread.join(timeout=5)

    def _lines(self, level=logging.WARNING):
        return [r for r in self.capture.records if r.levelno >= level]

    def test_turn_never_waits_for_a_slow_probe(self):
        release = threading.Event()

        def slow_probe(host):
            release.wait(5)  # a host that does not answer: ~2 s timeout in real life
            return ("unreachable", "host não responde (ConnectTimeout)")

        with patch.object(tracing, "_probe", side_effect=slow_probe):
            started = time.perf_counter()
            traces = [self._turn() for _ in range(3)]
            elapsed = time.perf_counter() - started
            self.assertLess(elapsed, 0.05, f"start_trace blocked for {elapsed:.3f}s")
            self.assertEqual(traces, [None] * 3)
            release.set()
            tracing._connect_thread.join(timeout=5)
        self.assertEqual(tracing.status()["status"], "unreachable")

    def test_self_host_down_warns_once_and_counts_every_skipped_turn(self):
        down = ("unreachable", "host não responde (ConnectError)")
        with patch.object(tracing, "_probe", return_value=down) as probe:
            self._connect_now()
            traces = [self._turn() for _ in range(5)]
        self.assertEqual(traces, [None] * 5)
        self.assertEqual(probe.call_count, 1, "probe is retried only after RETRY_SECONDS")
        self.assertEqual(FakeLangfuse.built, 0, "SDK is not touched when the host is down")
        warnings = self._lines()
        self.assertEqual(len(warnings), 1)
        self.assertIn("não responde", warnings[0].getMessage())
        self.assertEqual(observability.metrics.counters["langfuse_trace_skipped"], 5)
        self.assertEqual(observability.metrics.counters["langfuse_unavailable"], 1)
        self.assertEqual(tracing.status()["status"], "unreachable")

    def test_metrics_reason_does_not_expose_the_host(self):
        real_probe = tracing._probe
        with patch("httpx.get", side_effect=OSError("down")):
            self.assertIsNotNone(real_probe("http://langfuse.internal.example:3001"))
            with patch.object(tracing, "_host", return_value="http://langfuse.internal.example:3001"):
                self._connect_now()
        self.assertEqual(tracing.status()["status"], "unreachable")
        self.assertNotIn("internal.example", str(tracing.status()))

    def test_recovers_silently_after_retry_window(self):
        with patch.object(tracing, "_probe", return_value=("unreachable", "x")):
            self._connect_now()
            self.assertIsNone(self._turn())
        tracing._retry_at = 0.0  # retry window elapsed
        with patch.object(tracing, "_probe", return_value=None):
            self._connect_now()
            trace = self._turn()
        self.assertIsNotNone(trace)
        self.assertEqual(tracing.status()["status"], "ok")
        self.assertEqual(len(self._lines()), 1, "recovery does not add a warning")
        self.assertTrue(any("de novo" in r.getMessage() for r in self.capture.records if r.levelno == logging.INFO))

    def test_rejected_credentials_hide_the_sdk_error_and_shut_the_candidate_down(self):
        FakeLangfuse.auth_ok = False
        with patch.object(tracing, "_probe", return_value=None):
            self._connect_now()
            for _ in range(3):
                self.assertIsNone(self._turn())
        messages = [r.getMessage() for r in self._lines()]
        self.assertEqual(len(messages), 1)
        self.assertNotIn("Unexpected error", " ".join(messages), "SDK noise must not reach the app log")
        self.assertEqual(tracing.status()["status"], "auth")
        self.assertEqual(observability.metrics.counters["langfuse_sdk_errors"], 1)
        self.assertEqual(len(FakeLangfuse.instances), 1)
        self.assertTrue(_wait(lambda: FakeLangfuse.instances[0].shutdowns == 1), "rejected client leaked")

    def test_live_client_that_starts_failing_degrades_once_and_is_shut_down(self):
        with patch.object(tracing, "_probe", return_value=None):
            self._connect_now()
            self.assertIsNotNone(self._turn())
        live = FakeLangfuse.instances[0]
        for _ in range(4):  # background flushes failing after the self-host went down
            logging.getLogger("langfuse").error("Unexpected error occurred.")
        self.assertIsNone(self._turn())
        self.assertEqual(len(self._lines()), 1)
        self.assertEqual(observability.metrics.counters["langfuse_sdk_errors"], 4)
        self.assertEqual(tracing.status()["status"], "unreachable")
        self.assertTrue(_wait(lambda: live.shutdowns == 1), "dropped client must be shut down exactly once")

    def test_late_flush_of_a_dropped_client_does_not_degrade_the_new_one(self):
        old_flusher_ready, old_flusher_go, done = threading.Event(), threading.Event(), threading.Event()

        class ThreadedFake(FakeLangfuse):
            def __init__(self):
                super().__init__()
                if len(FakeLangfuse.instances) == 1:  # the first client owns a consumer thread
                    def consumer():
                        old_flusher_ready.set()
                        old_flusher_go.wait(5)
                        logging.getLogger("langfuse").error("Unexpected error occurred.")  # late flush
                        done.set()
                    threading.Thread(target=consumer, daemon=True).start()

        with patch.dict(sys.modules, {"langfuse": SimpleNamespace(Langfuse=ThreadedFake)}), \
                patch.object(tracing, "_probe", return_value=None):
            self._connect_now()
            old = FakeLangfuse.instances[0]
            self.assertTrue(old_flusher_ready.wait(2))
            tracing._degrade("unreachable", "envio de eventos falhou")  # old client dropped
            tracing._retry_at = 0.0
            self._connect_now()  # new client
            self.assertIsNotNone(self._turn())
            old_flusher_go.set()
            self.assertTrue(done.wait(2))
        self.assertEqual(tracing.status()["status"], "ok", "old client's flush degraded the new client")
        self.assertIs(tracing._client, FakeLangfuse.instances[1])
        self.assertTrue(_wait(lambda: old.shutdowns == 1))
        self.assertEqual(observability.metrics.counters["langfuse_sdk_errors"], 1)

    def test_without_keys_is_off_and_silent(self):
        with patch.dict(os.environ, {"LANGFUSE_PUBLIC_KEY": "", "LANGFUSE_SECRET_KEY": ""}), \
                patch.object(tracing, "_probe") as probe:
            self.assertIsNone(self._turn())
            self.assertIsNone(tracing.warm_up())
            self.assertEqual(tracing.status()["status"], "off")
        probe.assert_not_called()
        self.assertEqual(self._lines(), [])


if __name__ == "__main__":
    unittest.main()
