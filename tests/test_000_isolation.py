"""Test isolation from Langfuse, and proof that importing the API opens no connection.

Named test_000_* so `unittest discover` (sorted) imports it before any module that imports
backend.*: the empty keys below win over the local .env (load_dotenv never overrides).
pytest gets the same from tests/conftest.py.
"""
import os
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["LANGFUSE_PUBLIC_KEY"] = ""
os.environ["LANGFUSE_SECRET_KEY"] = ""

ROOT = Path(__file__).resolve().parents[1]


class ImportOpensNoConnectionTests(unittest.TestCase):
    def test_importing_backend_api_with_keys_starts_no_langfuse_connection(self):
        # Fresh interpreter, keys present (fake host): the import alone must not probe, build
        # a Langfuse client or start the connect thread.
        code = textwrap.dedent("""
            import threading
            from backend import tracing
            calls = []
            tracing._probe = lambda host: calls.append(host)
            import backend.api  # noqa: F401
            names = sorted(t.name for t in threading.enumerate())
            print(repr((calls, tracing._connect_thread, tracing._client, names)))
        """)
        env = {**os.environ, "LANGFUSE_PUBLIC_KEY": "pk-lf-test", "LANGFUSE_SECRET_KEY": "sk-lf-test",
               "LANGFUSE_HOST": "http://127.0.0.1:9", "ATLAS_PUBLIC_KEY": "t", "ATLAS_PRIVATE_KEY": "t",
               "ATLAS_ORG_ID": "t"}
        out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True,
                             text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        calls, thread, client, names = eval(out.stdout.strip().splitlines()[-1])  # noqa: S307 — our own repr
        self.assertEqual(calls, [])
        self.assertIsNone(thread)
        self.assertIsNone(client)
        self.assertNotIn("langfuse-connect", names)

    def test_server_startup_is_what_starts_the_connection(self):
        from fastapi.testclient import TestClient
        from backend import api, tracing
        with patch.object(tracing, "warm_up") as warm_up:
            TestClient(api.app)  # no lifespan without the context manager
            warm_up.assert_not_called()
            with TestClient(api.app):
                pass
            warm_up.assert_called_once()

    def test_suite_runs_with_tracing_off(self):
        from backend import tracing
        self.assertEqual(tracing.status()["status"], "off")


if __name__ == "__main__":
    unittest.main()
