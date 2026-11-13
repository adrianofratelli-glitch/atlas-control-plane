"""Langfuse fail-open without log noise: one warning per outage, then silence + counters.

Seen live on 2026-11-12/13: with the self-host down, the SDK printed
"Unexpected error occurred..." (logger `langfuse`) and the turns ran untraced.
These tests pin the contract of backend/tracing.py without any network.
"""
import logging
import os
import sys
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

    def __init__(self):
        FakeLangfuse.built += 1

    def auth_check(self):
        if not FakeLangfuse.auth_ok:
            # what the real SDK does on failure: logs through the `langfuse` logger
            logging.getLogger("langfuse").error("Unexpected error occurred. Please check your request.")
            return False
        return True

    def trace(self, **kwargs):
        return SimpleNamespace(**kwargs)


class TracingFailOpenTests(unittest.TestCase):
    def setUp(self):
        self.saved = (tracing._client, tracing._retry_at, dict(tracing._state))
        tracing._client, tracing._retry_at = None, 0.0
        tracing._state.update(status="pending", reason=None, warned=False)
        self.saved_counters = dict(observability.metrics.counters)
        observability.metrics.counters.clear()
        FakeLangfuse.auth_ok, FakeLangfuse.built = True, 0
        self.capture = _Capture()
        logging.getLogger().addHandler(self.capture)
        self.saved_level = tracing.logger.level
        tracing.logger.setLevel(logging.DEBUG)
        self.env = patch.dict(os.environ, ENV)
        self.env.start()
        self.fake_module = patch.dict(sys.modules, {"langfuse": SimpleNamespace(Langfuse=FakeLangfuse)})
        self.fake_module.start()

    def tearDown(self):
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

    def _lines(self, level=logging.WARNING):
        return [r for r in self.capture.records if r.levelno >= level]

    def test_self_host_down_warns_once_and_counts_every_skipped_turn(self):
        down = ("unreachable", "http://127.0.0.1:9 não responde (ConnectError)")
        with patch.object(tracing, "_probe", return_value=down) as probe:
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

    def test_recovers_silently_after_retry_window(self):
        with patch.object(tracing, "_probe", return_value=("unreachable", "x")):
            self.assertIsNone(self._turn())
        tracing._retry_at = 0.0  # retry window elapsed
        with patch.object(tracing, "_probe", return_value=None):
            trace = self._turn()
        self.assertIsNotNone(trace)
        self.assertEqual(tracing.status()["status"], "ok")
        self.assertEqual(len(self._lines()), 1, "recovery does not add a warning")
        self.assertTrue(any("de novo" in r.getMessage() for r in self.capture.records if r.levelno == logging.INFO))

    def test_rejected_credentials_hide_the_sdk_error_line(self):
        FakeLangfuse.auth_ok = False
        with patch.object(tracing, "_probe", return_value=None):
            for _ in range(3):
                self.assertIsNone(self._turn())
        messages = [r.getMessage() for r in self._lines()]
        self.assertEqual(len(messages), 1)
        self.assertNotIn("Unexpected error", " ".join(messages), "SDK noise must not reach the app log")
        self.assertEqual(tracing.status()["status"], "auth")
        self.assertEqual(observability.metrics.counters["langfuse_sdk_errors"], 1)

    def test_live_client_that_starts_failing_degrades_once(self):
        with patch.object(tracing, "_probe", return_value=None):
            self.assertIsNotNone(self._turn())
        for _ in range(4):  # background flushes failing after the self-host went down
            logging.getLogger("langfuse").error("Unexpected error occurred.")
        self.assertIsNone(self._turn())
        self.assertEqual(len(self._lines()), 1)
        self.assertEqual(observability.metrics.counters["langfuse_sdk_errors"], 4)
        self.assertEqual(tracing.status()["status"], "unreachable")

    def test_without_keys_is_off_and_silent(self):
        with patch.dict(os.environ, {"LANGFUSE_PUBLIC_KEY": "", "LANGFUSE_SECRET_KEY": ""}), \
                patch.object(tracing, "_probe") as probe:
            self.assertIsNone(self._turn())
            self.assertEqual(tracing.status()["status"], "off")
        probe.assert_not_called()
        self.assertEqual(self._lines(), [])


if __name__ == "__main__":
    unittest.main()
