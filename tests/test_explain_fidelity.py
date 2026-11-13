"""The explain the Profiler shows is the explain that ran.

Before 2026-11-13: /api/explain sent only the filter (the captured sort was lost) and
looked for the index one level deep, so SORT -> FETCH -> IXSCAN was reported as
"COLLSCAN (sem índice)". Now the endpoint echoes the exact command and walks the plan.
"""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("ATLAS_PUBLIC_KEY", "test")
os.environ.setdefault("ATLAS_PRIVATE_KEY", "test")
os.environ.setdefault("ATLAS_ORG_ID", "test")

from fastapi.testclient import TestClient  # noqa: E402

from backend import api  # noqa: E402

NESTED = {
    "queryPlanner": {"winningPlan": {"stage": "SORT", "inputStage": {
        "stage": "FETCH", "inputStage": {"stage": "IXSCAN", "indexName": "account_number_1"}}}},
    "executionStats": {"totalDocsExamined": 12, "totalKeysExamined": 12, "nReturned": 12, "executionTimeMillis": 3},
}
SBE = {"queryPlanner": {"winningPlan": {"queryPlan": {"stage": "FETCH", "inputStage": {
    "stage": "IXSCAN", "indexName": "ts_1"}}}}, "executionStats": {}}
COLLSCAN = {"queryPlanner": {"winningPlan": {"stage": "COLLSCAN"}}, "executionStats": {"totalDocsExamined": 900}}

BODY = {"namespace": "banco_inter_test.fatura", "filter": {"account_number": 7},
        "project_id": "abc123", "cluster_name": "c1"}


class SummarizeExplainTests(unittest.TestCase):
    def test_index_below_sort_and_fetch_is_found(self):
        out = api.summarize_explain(NESTED)
        self.assertEqual(out["index_used"], "account_number_1")
        self.assertEqual(out["stage"], "SORT → FETCH → IXSCAN")

    def test_sbe_query_plan_is_walked(self):
        self.assertEqual(api.summarize_explain(SBE)["index_used"], "ts_1")

    def test_collscan_is_still_reported(self):
        self.assertEqual(api.summarize_explain(COLLSCAN)["index_used"], "COLLSCAN (sem índice)")


class ExplainEndpointTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(api.app)
        self.mc = MagicMock()
        self.mc.__getitem__.return_value.command.return_value = NESTED
        self.patches = [patch.dict(os.environ, {"MONGODB_URI": "mongodb://x"}),
                        patch("backend.api._assert_uri_targets"),
                        patch("backend.api._mongo", return_value=self.mc)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    def test_sort_and_limit_reach_the_driver_and_the_response_echoes_them(self):
        r = self.client.post("/api/explain", json={**BODY, "sort": {"due_date": -1}, "limit": 20})
        self.assertEqual(r.status_code, 200, r.text)
        sent = self.mc.__getitem__.return_value.command.call_args.args[1]
        self.assertEqual(sent, {"find": "fatura", "filter": {"account_number": 7}, "sort": {"due_date": -1},
                                "limit": 20, "maxTimeMS": 10000})
        data = r.json()
        self.assertEqual(data["command"], {"explain": sent, "verbosity": "executionStats"})
        self.assertEqual(data["database"], "banco_inter_test")
        self.assertEqual(data["index_used"], "account_number_1")

    def test_filter_only_explain_keeps_the_old_shape(self):
        r = self.client.post("/api/explain", json=BODY)
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("sort", r.json()["command"]["explain"])

    def test_hostile_sort_is_rejected(self):
        for sort in ({"a": "hashed"}, {"$where": 1}, {"a.$b": 1}):
            r = self.client.post("/api/explain", json={**BODY, "sort": sort})
            self.assertEqual(r.status_code, 400, sort)
        r = self.client.post("/api/explain", json={**BODY, "limit": 1000})
        self.assertEqual(r.status_code, 422)


if __name__ == "__main__":
    unittest.main()
