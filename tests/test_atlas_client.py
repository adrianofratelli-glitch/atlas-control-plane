"""Unit tests for AtlasClient.recommend_scaling — the core "intelligence" of
the Scale page. Pure function (no network), so no mocking needed."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from atlas_client import AtlasClient


class TestRecommendScaling(unittest.TestCase):
    def test_no_measurements_returns_ok(self):
        result = AtlasClient.recommend_scaling({}, "M10")
        self.assertEqual(result["action"], "ok")

    def test_measurements_error_returns_ok(self):
        result = AtlasClient.recommend_scaling({"error": "timeout"}, "M10")
        self.assertEqual(result["action"], "ok")

    def test_high_cpu_recommends_scale_up_high(self):
        result = AtlasClient.recommend_scaling({"cpu_pct": 85, "connections": 0, "mem_pct": 10, "disk_pct": 10}, "M10")
        self.assertEqual(result["action"], "up")
        self.assertEqual(result["severity"], "high")

    def test_medium_cpu_recommends_scale_up_med(self):
        result = AtlasClient.recommend_scaling({"cpu_pct": 70, "connections": 0, "mem_pct": 10, "disk_pct": 10}, "M10")
        self.assertEqual(result["action"], "up")
        self.assertEqual(result["severity"], "med")

    def test_low_utilization_recommends_scale_down(self):
        result = AtlasClient.recommend_scaling(
            {"cpu_pct": 5, "connections": 10, "mem_pct": 20, "disk_pct": 20}, "M20"
        )
        self.assertEqual(result["action"], "down")

    def test_m10_never_recommended_down(self):
        """M10 is the smallest dedicated tier — there is nothing smaller to scale to."""
        result = AtlasClient.recommend_scaling(
            {"cpu_pct": 5, "connections": 10, "mem_pct": 20, "disk_pct": 20}, "M10"
        )
        self.assertNotEqual(result["action"], "down")

    def test_healthy_load_recommends_ok(self):
        result = AtlasClient.recommend_scaling(
            {"cpu_pct": 40, "connections": 100, "mem_pct": 50, "disk_pct": 40}, "M30"
        )
        self.assertEqual(result["action"], "ok")

    def test_cpu24_p95_used_for_scale_up_decision(self):
        """Scale-up must react to sustained p95, not a momentarily-quiet snapshot."""
        result = AtlasClient.recommend_scaling(
            {"cpu_pct": 10, "connections": 0, "mem_pct": 10, "disk_pct": 10}, "M10",
            cpu24={"avg": 20, "p95": 90},
        )
        self.assertEqual(result["action"], "up")

    def test_cpu24_avg_used_for_scale_down_decision(self):
        """Scale-down must react to the 24h average, not a single low reading."""
        result = AtlasClient.recommend_scaling(
            {"cpu_pct": 5, "connections": 10, "mem_pct": 20, "disk_pct": 20}, "M20",
            cpu24={"avg": 60, "p95": 65},
        )
        self.assertNotEqual(result["action"], "down")

    def test_high_connection_usage_recommends_scale_up(self):
        result = AtlasClient.recommend_scaling(
            {"cpu_pct": 10, "connections": 1300, "mem_pct": 10, "disk_pct": 10}, "M10"
        )
        self.assertEqual(result["action"], "up")


class TestEstimateCost(unittest.TestCase):
    def test_known_tier(self):
        cost = AtlasClient.estimate_cost("M10", usd_brl=5.0)
        self.assertEqual(cost["usd"], 57)
        self.assertEqual(cost["brl"], 285)

    def test_unknown_tier_defaults_to_zero(self):
        cost = AtlasClient.estimate_cost("M9999", usd_brl=5.0)
        self.assertEqual(cost["usd"], 0)


if __name__ == "__main__":
    unittest.main()
