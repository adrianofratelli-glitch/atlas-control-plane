import unittest
from unittest.mock import patch
from atlas_client import AtlasClient
from cluster_insights import summarize, stats


def node(name, cpu, points=288):
    return {'alias': name, 'role': 'REPLICA_SECONDARY',
            'cpu_24h': stats([cpu] * points), 'cpu_recent': stats([cpu] * 6),
            'series': {'timestamps': list(range(points)), 'cpu': [cpu] * points},
            'snapshot': {'_disk_available': True, '_memory_available': True, 'mem_pct': 20,
                         'disk_pct': 20, 'connections': 10, 'disk_iops_read': 5, 'disk_iops_write': 5, '_raw': {'CONNECTIONS': 10}}}


class InsightsTests(unittest.TestCase):
    def test_average_does_not_hide_hot_node(self):
        r = summarize([node('a', 90), node('b', 5), node('c', 5)], 3, 'M20')
        self.assertEqual(r['cpu'], 33.3)
        self.assertEqual(r['action'], 'up')
        self.assertEqual(r['hottest_node'], 'a')

    def test_partial_history_blocks_down(self):
        r = summarize([node('a', 5, 20), node('b', 5, 20), node('c', 5, 20)], 3, 'M20')
        self.assertFalse(r['complete'])
        self.assertEqual(r['action'], 'unknown')

    def test_missing_node_blocks_down(self):
        self.assertNotEqual(summarize([node('a', 5), node('b', 5)], 3, 'M20')['action'], 'down')

    def test_complete_low_load_is_conditional_candidate(self):
        self.assertEqual(summarize([node('a', 5), node('b', 5), node('c', 5)], 3, 'M20')['action'], 'down')

    def test_recent_pressure_blocks_down(self):
        nodes = [node('a', 5), node('b', 5), node('c', 5)]
        nodes[0]['cpu_recent'] = stats([90] * 6)
        self.assertEqual(summarize(nodes, 3, 'M20')['action'], 'up')

    def test_missing_recent_data_blocks_down(self):
        nodes = [node('a', 5), node('b', 5), node('c', 5)]
        nodes[0]['cpu_recent'] = None
        self.assertNotEqual(summarize(nodes, 3, 'M20')['action'], 'down')

    def test_missing_disk_blocks_down(self):
        nodes = [node('a', 5), node('b', 5), node('c', 5)]
        nodes[0]['snapshot']['_disk_available'] = False
        self.assertNotEqual(summarize(nodes, 3, 'M20')['action'], 'down')

    def test_shared_dns_suffix_is_not_identity(self):
        client = AtlasClient('test', 'test', 'test')
        processes = [dict(id='wrong', hostname='atlas-old.shared.mongodb.net', userAlias='other-shard-00-00.shared.mongodb.net', typeName='REPLICA_PRIMARY'),
                     dict(id='correct', hostname='atlas-new.shared.mongodb.net', userAlias='inter-shard-00-00.shared.mongodb.net', typeName='REPLICA_PRIMARY')]
        with patch.object(client, 'get_cluster', return_value={'connectionStrings': {'standardSrv': 'mongodb+srv://inter.shared.mongodb.net'}}), patch.object(client, 'get_processes', return_value=processes):
            self.assertEqual(client._get_primary_uncached('p', 'inter'), 'correct')
            self.assertIsNone(client._get_primary_uncached('p', 'missing'))

    def test_series_null_and_timestamp_alignment(self):
        client = AtlasClient('test', 'test', 'test')
        data = {'measurements': [
            {'name': 'SYSTEM_NORMALIZED_CPU_USER', 'dataPoints': [{'timestamp':'a','value':10}, {'timestamp':'b','value':None}]},
            {'name': 'SYSTEM_NORMALIZED_CPU_KERNEL', 'dataPoints': [{'timestamp':'b','value':None}, {'timestamp':'a','value':2}]}]}
        with patch.object(client, '_get', return_value=data):
            self.assertEqual(client.get_measurements_series('p', 'h')['cpu'], [12, None])


class StreamAndBillingTests(unittest.TestCase):
    def test_model_limit_has_error_and_terminal_done(self):
        import json
        import os
        import api
        import assistant_api
        from fastapi.testclient import TestClient
        async def limited(*args, **kwargs):
            yield {"type": "text", "text": "partial"}
            yield {"type": "error", "message": "limite de tamanho"}
        headers = {"Authorization": "Bearer " + os.environ["API_AUTH_TOKEN"]} if os.getenv("API_AUTH_TOKEN") else {}
        with patch.object(assistant_api, "save_user", return_value=None), patch.object(assistant_api, "run_assistant", limited):
            response = TestClient(api.app).post('/api/assistant', headers=headers, json={
                'session_id': 'x' * 32, 'project_id': 'p', 'cluster_name': 'c',
                'messages': [{'role': 'user', 'content': 'report'}]})
        self.assertEqual(response.status_code, 200)
        events = [json.loads(line) for line in response.text.splitlines()]
        self.assertEqual([e['type'] for e in events], ['text', 'error', 'done'])
        self.assertEqual(events[-2]['message'], 'limite de tamanho')

    def test_missing_billing_org_is_not_zero_invoice(self):
        import api
        from unittest.mock import MagicMock
        client = MagicMock(org_id='')
        with patch.object(api, 'get_client', return_value=client):
            self.assertIsNone(api.invoice()['amount_usd'])
        client.get_pending_invoice.assert_not_called()


class ExplainTests(unittest.TestCase):
    def test_explain_preserves_real_sort_and_limit(self):
        from assistant_tools import ClusterTools
        from unittest.mock import MagicMock
        tools = ClusterTools('p', 'inter')
        mongo = MagicMock()
        args = {'namespace': 'banco_inter.transacoes', 'filter': {'account_number': 'demo'},
                'sort': {'amos_mt_eff_date': -1}, 'limit': 50}
        tools.validate('mongo_explain', args)
        with patch.object(tools, 'mongo', return_value=mongo):
            tools.read('mongo_explain', args)
        query = mongo['banco_inter'].command.call_args.args[1]
        self.assertEqual(query['sort'], args['sort'])
        self.assertEqual(query['limit'], 50)
