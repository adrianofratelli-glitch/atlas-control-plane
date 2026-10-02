import asyncio
import json
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from metrics_cache import MetricsCache
import assistant_report


class Stream:
    async def __aenter__(self): return self
    async def __aexit__(self, *args): return False
    @property
    async def text_stream(self):
        yield 'Relatório com evidências.'
    async def get_final_message(self): return SimpleNamespace(stop_reason='end_turn', usage=None)


class ReportTests(unittest.IsolatedAsyncioTestCase):
    async def test_report_reads_only_via_mcp_and_calls_model_once(self):
        calls, events = [], []
        async def call(name, args):
            calls.append(name)
            data = {'tier': 'M20', 'marker': name}
            return SimpleNamespace(content=[SimpleNamespace(type='text', text=json.dumps(data))], isError=False)
        client = SimpleNamespace(messages=MagicMock())
        client.messages.stream.return_value = Stream()
        async def emit(event): events.append(event)
        result = await assistant_report.run_report(mcp=SimpleNamespace(call_tool=call), client=client, system='system', request_text='relatório', emit=emit)
        self.assertEqual(set(calls), {'atlas_cluster_insights','atlas_cluster','atlas_indexes','atlas_slow_queries','atlas_cost'})
        self.assertEqual(result, 'Relatório com evidências.')
        client.messages.stream.assert_called_once()
        kwargs = client.messages.stream.call_args.kwargs
        self.assertNotIn('tools', kwargs)
        for name in calls: self.assertIn(name, kwargs['messages'][0]['content'])
        self.assertEqual(events[-1]['type'], 'done')
        self.assertEqual(len([e for e in events if e['type']=='tool_end']), 5)

    async def test_tool_timeout_becomes_missing_evidence_not_silence(self):
        events = []
        async def call(name, args): await asyncio.sleep(1)
        async def emit(event): events.append(event)
        client = SimpleNamespace(messages=MagicMock())
        client.messages.stream.return_value = Stream()
        with patch.object(assistant_report, 'TOOL_TIMEOUT', .01):
            await assistant_report.run_report(mcp=SimpleNamespace(call_tool=call), client=client, system='system', request_text='relatório', emit=emit)
        failures = [e for e in events if e['type']=='tool_end']
        self.assertTrue(all(not e['ok'] for e in failures))
        self.assertEqual(events[-1]['type'], 'done')
        self.assertIn('Coleta indisponível', client.messages.stream.call_args.kwargs['messages'][0]['content'])


class CacheTests(unittest.TestCase):
    def test_concurrent_polling_fetches_atlas_once(self):
        cache, count = MetricsCache(), []
        def fetch():
            count.append(1); time.sleep(.02)
            return {'fetched_at':'original'}
        with ThreadPoolExecutor(max_workers=8) as pool:
            rows = list(pool.map(lambda _: cache.get('cluster', 60, fetch), range(8)))
        self.assertEqual(len(count), 1)
        self.assertTrue(all(row['fetched_at']=='original' for row in rows))

    def test_expiry_refreshes_and_keeps_clusters_isolated(self):
        cache = MetricsCache()
        self.assertEqual(cache.get('a', 0, lambda: 1), 1)
        self.assertEqual(cache.get('a', 60, lambda: 2), 2)
        self.assertEqual(cache.get('b', 60, lambda: 3), 3)
