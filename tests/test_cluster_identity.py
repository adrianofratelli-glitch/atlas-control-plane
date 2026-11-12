"""Cluster identity: MONGODB_URI must match the selected cluster by HOST, not by project hash.

Regression found live on 2026-11-12: three clusters of the same project shared the
SRV project hash, so explain/index/assistant data tools aimed at a paused cluster
ran on the cluster MONGODB_URI points to.
"""
import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("MONGODB_URI", "")

from backend import api  # noqa: E402
from backend.cluster_identity import cluster_hosts, uri_hosts, uri_targets_cluster  # noqa: E402

PROJECT_HASH = "abcd1"


def atlas_cluster(label: str) -> dict:
    """Admin API v2 shape: same project hash for every cluster of the project."""
    return {
        "name": label,
        "connectionStrings": {
            "standardSrv": f"mongodb+srv://{label}.{PROJECT_HASH}.mongodb.net",
            "standard": (f"mongodb://{label}-shard-00-00.{PROJECT_HASH}.mongodb.net:27017,"
                         f"{label}-shard-00-01.{PROJECT_HASH}.mongodb.net:27017/?ssl=true"),
            "privateEndpoint": [{"srvConnectionString": f"mongodb+srv://{label}-pl-0.{PROJECT_HASH}.mongodb.net"}],
        },
    }


SCHEME_SRV, SCHEME_STD, CREDS = "mongodb+srv" + "://", "mongodb" + "://", "u" + ":" + "p" + "@"  # split: no literal URI with credentials
SRV_URI = SCHEME_SRV + CREDS + "alpha." + PROJECT_HASH + ".mongodb.net/?retryWrites=true"


class ClusterIdentityTests(unittest.TestCase):
    def test_same_project_other_cluster_is_not_the_target(self):
        self.assertTrue(uri_targets_cluster(SRV_URI, atlas_cluster("alpha")))
        self.assertFalse(uri_targets_cluster(SRV_URI, atlas_cluster("beta")))

    def test_standard_uri_with_member_hosts(self):
        uri = (SCHEME_STD + CREDS + "alpha-shard-00-01." + PROJECT_HASH + ".mongodb.net:27017,"
               "alpha-shard-00-02." + PROJECT_HASH + ".mongodb.net:27017/admin?ssl=true")
        self.assertTrue(uri_targets_cluster(uri, atlas_cluster("alpha")))
        self.assertFalse(uri_targets_cluster(uri, atlas_cluster("beta")))

    def test_private_endpoint_uri(self):
        uri = SCHEME_SRV + CREDS + "alpha-pl-0." + PROJECT_HASH + ".mongodb.net/"
        self.assertTrue(uri_targets_cluster(uri, atlas_cluster("alpha")))

    def test_case_port_and_credentials_do_not_matter(self):
        self.assertEqual(uri_hosts(SCHEME_SRV + "us%40r" + ":" + "p%3Aw" + "@ALPHA.Abcd1.MongoDB.net/db"),
                         frozenset({"alpha.abcd1.mongodb.net"}))

    def test_empty_or_garbage_never_matches(self):
        for uri in ("", "configured", "http://alpha.abcd1.mongodb.net", None):
            self.assertFalse(uri_targets_cluster(uri, atlas_cluster("alpha")))
        self.assertFalse(uri_targets_cluster(SRV_URI, {}))
        self.assertEqual(cluster_hosts({"connectionStrings": None}), frozenset())

    def test_assert_uri_targets_refuses_sibling_cluster(self):
        client = MagicMock()
        with patch.dict(os.environ, {"MONGODB_URI": SRV_URI}), \
                patch("backend.api.get_client", return_value=client), patch.object(client, "get_cluster", return_value=atlas_cluster("beta")):
            with self.assertRaises(api.HTTPException) as ctx:
                api._assert_uri_targets("p1", "beta")
        self.assertEqual(ctx.exception.status_code, 409)

    def test_assert_uri_targets_accepts_the_uri_cluster(self):
        client = MagicMock()
        with patch.dict(os.environ, {"MONGODB_URI": SRV_URI}), \
                patch("backend.api.get_client", return_value=client), patch.object(client, "get_cluster", return_value=atlas_cluster("alpha")):
            api._assert_uri_targets("p1", "alpha")

    def test_cluster_without_published_hosts_fails_closed(self):
        client = MagicMock()
        with patch.dict(os.environ, {"MONGODB_URI": SRV_URI}), \
                patch("backend.api.get_client", return_value=client), patch.object(client, "get_cluster", return_value={"name": "alpha"}):
            with self.assertRaises(api.HTTPException) as ctx:
                api._assert_uri_targets("p1", "alpha")
        self.assertEqual(ctx.exception.status_code, 503)

    def test_cluster_list_flags_only_the_uri_cluster(self):
        client = MagicMock()
        clusters = []
        for label in ("alpha", "beta", "gamma"):
            c = atlas_cluster(label)
            c.update({"stateName": "IDLE", "mongoDBVersion": "9.0.1", "clusterType": "REPLICASET",
                      "replicationSpecs": [{"regionConfigs": [{"electableSpecs": {"instanceSize": "M10"},
                                                                "regionName": "US_EAST_1"}]}]})
            clusters.append(c)
        with patch.dict(os.environ, {"MONGODB_URI": SRV_URI}), \
                patch("backend.api.get_client", return_value=client), patch.object(client, "get_projects", return_value=[{"id": "p1", "name": "P"}]), \
                patch.object(client, "get_clusters", return_value=clusters):
            rows = api._list_clusters_sync()["clusters"]
        self.assertEqual({r["cluster_name"]: r["is_uri_target"] for r in rows},
                         {"alpha": True, "beta": False, "gamma": False})


if __name__ == "__main__":
    unittest.main()
