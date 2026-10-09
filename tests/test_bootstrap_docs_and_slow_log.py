"""Regressões da rodada 2026-10-09.

- Bootstrap documentado igual ao comportamento real: LLM só via Grove (nenhum doc manda
  preencher ANTHROPIC_API_KEY) e todo `docker build` passa o contexto `shared`.
- `/api/cluster/.../slow` não devolve mais o slow log inteiro (~12 MB): pede `nLogs` ao
  Atlas e corta no servidor mesmo que o Atlas ignore o parâmetro.
"""
import json
import os
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("API_AUTH_TOKEN", "")
os.environ["MONGODB_URI"] = ""

import requests  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import api  # noqa: E402

# CLAUDE.md/AGENTS.md ficam fora do Git (gitignore global), mas são lidos por agentes: entram
# na checagem quando existem localmente.
DOC_FILES = ["README.md", "Dockerfile", ".env.example", "run_react.sh", "CLAUDE.md", "AGENTS.md",
             "implementation_plan.md", *[str(p.relative_to(ROOT)) for p in (ROOT / "docs").rglob("*.md")]]


def _docs():
    for name in DOC_FILES:
        path = ROOT / name
        if path.is_file():
            yield name, path.read_text(encoding="utf-8")


class BootstrapDocsTests(unittest.TestCase):
    def test_no_doc_asks_for_a_direct_provider_key(self):
        offenders = []
        for name, text in _docs():
            for line in text.splitlines():
                if "ANTHROPIC_API_KEY" in line and not re.search(r"n[ãa]o existe|\bsem\b|no fallback|nunca|never|not ", line, re.I):
                    offenders.append(f"{name}: {line.strip()}")
        self.assertEqual(offenders, [])

    def test_env_example_lists_grove_not_provider(self):
        text = (ROOT / ".env.example").read_text(encoding="utf-8")
        keys = {line.split("=", 1)[0].lstrip("# ").strip() for line in text.splitlines() if "=" in line}
        self.assertIn("GROVE_BASE_URL", keys)
        self.assertIn("GROVE_API_KEY", keys)
        self.assertNotIn("ANTHROPIC_API_KEY", keys)

    def test_every_docker_build_passes_the_shared_context(self):
        self.assertIn("COPY --from=shared", (ROOT / "Dockerfile").read_text(encoding="utf-8"))
        offenders = [f"{name}: {line.strip()}" for name, text in _docs() for line in text.splitlines()
                     if "docker build" in line and "--build-context shared=" not in line]
        self.assertEqual(offenders, [])


class SlowLogCapTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"ATLAS_PUBLIC_KEY": "x", "ATLAS_PRIVATE_KEY": "y", "ATLAS_ORG_ID": "o"})
        self.env.start()
        self.client = TestClient(api.app, raise_server_exceptions=False)
        self.params = []

    def tearDown(self):
        self.env.stop()

    def _fake_get(self, n_lines):
        def get(session, url, **kw):
            r = requests.Response()
            r.status_code, r.url = 200, url
            self.params.append(kw.get("params") or {})
            # Atlas "ignora" nLogs aqui: devolve tudo, o servidor tem de cortar.
            body = {"slowQueries": [{"namespace": "db.c", "line": "{}"}] * n_lines}
            r._content = json.dumps(body).encode()
            return r
        return get

    def _get(self, path):
        with patch.object(requests.Session, "get", self._fake_get(4312)), \
                patch.object(type(api.get_client()), "get_primary", return_value="h:27017"):
            return self.client.get(path)

    def test_default_limit_is_sent_to_atlas_and_enforced(self):
        r = self._get("/api/cluster/abc123/c1/slow")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(len(body["slowQueries"]), api.SLOW_LOG_DEFAULT_LINES)
        self.assertTrue(body["truncated"])
        self.assertEqual(self.params[-1], {"nLogs": api.SLOW_LOG_DEFAULT_LINES})

    def test_limit_is_bounded(self):
        self.assertEqual(self._get("/api/cluster/abc123/c1/slow?limit=50").json()["slowQueries"].__len__(), 50)
        for bad in ("0", "-1", str(api.SLOW_LOG_MAX_LINES + 1), "abc"):
            self.assertEqual(self._get(f"/api/cluster/abc123/c1/slow?limit={bad}").status_code, 422, bad)


class ScalingTierTests(unittest.TestCase):
    """`/scaling` sem `tier` usava 422; agora resolve o tier do próprio cluster."""

    def setUp(self):
        self.env = patch.dict(os.environ, {"ATLAS_PUBLIC_KEY": "x", "ATLAS_PRIVATE_KEY": "y", "ATLAS_ORG_ID": "o"})
        self.env.start()
        self.client = TestClient(api.app, raise_server_exceptions=False)
        api.metrics_cache._entries.clear() if hasattr(api.metrics_cache, "_entries") else None

    def tearDown(self):
        self.env.stop()

    def _run(self, path):
        seen = {}
        cluster = {"replicationSpecs": [{"regionConfigs": [{"electableSpecs": {"instanceSize": "M20"}}]}]}

        def fake_collect(client, project_id, cluster_name, tier):
            seen["tier"] = tier
            return {"nodes": [], "verdict": "ok", "action": "keep", "worst_node_p95": None, "cpu": None}

        with patch.object(type(api.get_client()), "get_cluster", return_value=cluster), \
                patch.object(api, "collect_insights", fake_collect):
            return self.client.get(path), seen

    def test_missing_tier_is_resolved_from_the_cluster(self):
        r, seen = self._run("/api/cluster/abc123/c-notier/scaling")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(seen["tier"], "M20")
        self.assertEqual(r.json()["tier"], "M20")

    def test_explicit_tier_is_kept_and_bounded(self):
        r, seen = self._run("/api/cluster/abc123/c-tier/scaling?tier=M30")
        self.assertEqual((r.status_code, seen["tier"]), (200, "M30"))
        r, _ = self._run("/api/cluster/abc123/c-tier/scaling?tier=" + "M" * 33)
        self.assertEqual(r.status_code, 422)


if __name__ == "__main__":
    unittest.main()
