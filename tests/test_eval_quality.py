"""Unit tests for eval/run_quality.py (offline): golden passes today and the eval detects regressions."""
import importlib.util
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ['MONGODB_URI'] = ''
ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location('torre_run_quality', ROOT / 'eval' / 'run_quality.py')
rq = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rq)


class EvalQualityTests(unittest.TestCase):
    def test_golden_is_draft_and_ids_unique(self):
        golden = json.loads((ROOT / 'eval' / 'golden.json').read_text(encoding='utf-8'))
        self.assertIn('RASCUNHO', golden['_status'])
        ids = [c['id'] for c in golden['casos']]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(15 <= len(ids) <= 40)

    def test_null_mask_is_exact_and_deterministic(self):
        self.assertEqual(sum(rq.null_mask(288, 0.5)), 144)
        self.assertEqual(sum(rq.null_mask(10, 0.4)), 4)
        self.assertEqual(sum(rq.null_mask(288, 0.0)), 0)

    def test_current_code_passes_golden(self):
        s = rq.run()['summary']
        self.assertEqual(s['accuracy'], 1.0)
        self.assertEqual((s['unsafe_accepted'], s['safe_blocked'], s['false_scale_down']), (0, 0, 0))

    def test_detects_heuristic_that_always_scales_down(self):
        down = {'action': 'down', 'severity': 'low', 'reasons': ['x'], 'headline': ''}
        with patch('backend.atlas_client.AtlasClient.recommend_scaling', return_value=down):
            s = rq.run()['summary']
        self.assertLess(s['groups']['escala']['accuracy'], 1.0)
        self.assertGreater(s['false_scale_down'], 0)

    def test_detects_guard_that_allows_everything(self):
        with patch('backend.assistant_tools.ClusterTools.validate', return_value=None):
            s = rq.run()['summary']
        self.assertGreater(s['unsafe_accepted'], 0)
        self.assertEqual(s['safe_blocked'], 0)

    def test_detects_null_points_turned_into_zero(self):
        real = rq.null_mask
        with patch.object(rq, 'null_mask', side_effect=lambda n, f: [False] * n if f else real(n, f)):
            s = rq.run()['summary']
        # Filling the gaps restores full coverage: the null cases (s07, c02) must now fail.
        self.assertLess(s['groups']['insights']['accuracy'], 1.0)
        self.assertLess(s['groups']['coleta']['accuracy'], 1.0)


if __name__ == '__main__':
    unittest.main()
