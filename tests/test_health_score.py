"""Health Score reads cluster status and version from Atlas, not from the caller."""
import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("MONGODB_URI", "")

from backend import api  # noqa: E402


def fake_client(cluster):
    client = MagicMock()
    client.get_cluster.return_value = cluster
    client.get_primary.return_value = "host-00:27017"
    client.get_suggested_indexes.return_value = {"suggestedIndexes": []}
    client.get_slow_queries.return_value = {"slowQueries": []}
    return client


class HealthScoreSourceTests(unittest.TestCase):
    def test_server_side_status_and_version_win_over_missing_params(self):
        client = fake_client({"stateName": "IDLE", "mongoDBVersion": "9.0.7"})
        with patch("backend.api.get_client", return_value=client):
            out = api.health("p1", "c1")  # no status/mongo_version, as an API caller would
        self.assertEqual(out["score"], 100)
        labels = {c["label"]: c for c in out["components"]}
        self.assertTrue(labels["Status do Cluster"]["ok"])
        self.assertEqual(labels["Versão do MongoDB"]["detail"], "MongoDB 9.0.7")

    def test_caller_cannot_inflate_the_score(self):
        client = fake_client({"stateName": "UPDATING", "mongoDBVersion": "6.0.20"})
        with patch("backend.api.get_client", return_value=client):
            out = api.health("p1", "c1", status="IDLE", mongo_version="9.0.0")
        self.assertEqual(out["score"], 80)  # 10 status + 30 + 30 + 0 version + 10 base (hints alone: 100)

    def test_atlas_read_failure_falls_back_to_hints(self):
        client = fake_client(None)
        client.get_cluster.side_effect = RuntimeError("Admin API down")
        with patch("backend.api.get_client", return_value=client):
            out = api.health("p1", "c1", status="IDLE", mongo_version="8.0.1")
        self.assertEqual(out["score"], 100)


if __name__ == "__main__":
    unittest.main()
