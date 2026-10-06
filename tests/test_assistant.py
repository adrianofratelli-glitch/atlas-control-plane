import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock
from bson import ObjectId
from fastapi.testclient import TestClient
from assistant_tools import ClusterTools, TOOLS, validate_pipeline
from assistant_actions import ActionStore
from torre_mcp_server import bounded_result
import api
import assistant_api


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.tools = ClusterTools('project', 'cluster')

    def test_all_advisor_indexes_are_retrievable(self):
        indexes = [{'index': [{'field'+str(i): 1}]} for i in range(107)]
        with patch('api.get_client') as get:
            get.return_value.get_primary.return_value = 'primary'
            get.return_value.get_suggested_indexes.return_value = {'suggestedIndexes': indexes}
            rows, offset = [], 0
            while True:
                result = self.tools.call('atlas_indexes', {'offset': offset})
                rows += result['items']
                if not result['has_more']: break
                offset = result['next_offset']
            self.assertEqual(indexes, rows)
            self.assertEqual(get.return_value.get_suggested_indexes.call_count, 6)

    def test_advisor_errors_are_not_empty_recommendations(self):
        with patch('api.get_client') as get:
            get.return_value.get_primary.return_value = 'primary'
            for data in [{'error': 'forbidden'}, {}]:
                get.return_value.get_suggested_indexes.return_value = data
                with self.assertRaises(ValueError): self.tools.call('atlas_indexes', {})

    def test_missing_cluster_has_actionable_message(self):
        with self.assertRaisesRegex(ValueError, 'Selecione um cluster'):
            ClusterTools('', '').call('mongo_databases', {})

    def test_tool_cannot_override_cluster_or_inject_execution_flag(self):
        from jsonschema import ValidationError
        for extra in [{'cluster_name': 'other'}, {'execute': True}, {'approved': True}]:
            with self.assertRaises(ValidationError): self.tools.call('mongo_databases', extra)

    def test_nested_write_stages_and_internal_collections_are_blocked(self):
        bad = [
            [{'$out': 'other'}], [{'$merge': 'other'}],
            [{'$facet': {'x': [{'$out': 'other'}]}}],
            [{'$lookup': {'from': 'chat_history', 'pipeline': []}}],
            [{'$match': {'$expr': {'$function': {'body': 'evil'}}}}],
            [{'$unionWith': {'db': 'admin', 'coll': 'users'}}],
        ]
        for pipeline in bad:
            with self.subTest(pipeline=pipeline), self.assertRaises((ValueError, api.HTTPException)):
                validate_pipeline(pipeline, 'torre')
        validate_pipeline([{'$match': {'$expr': {'$gt': ['$amount', 10]}}}, {'$group': {'_id': '$status', 'n': {'$sum': 1}}}], 'orders')

    def test_mutation_tool_only_prepares_and_ids_are_frozen(self):
        mc = MagicMock(); coll = mc.__getitem__.return_value.__getitem__.return_value
        ids = [ObjectId(), ObjectId()]
        coll.find.return_value.limit.return_value.max_time_ms.return_value = [{'_id': i} for i in ids]
        with patch.object(self.tools, 'mongo', return_value=mc):
            proposal = self.tools.call('mongo_delete', {'namespace': 'demo.orders', 'filter': {'status': 'test'}})
            coll.delete_many.assert_not_called()
            self.assertEqual(proposal['details']['documents'], 2)
            coll.delete_many.return_value.deleted_count = 2
            self.tools.execute('mongo_delete', proposal['arguments'], proposal['frozen_ids'])
        self.assertEqual(coll.delete_many.call_args.args[0], {'$and': [{'status': 'test'}, {'_id': {'$in': ids}}]})

    def test_batch_too_large_never_prepares_write(self):
        mc = MagicMock(); coll = mc.__getitem__.return_value.__getitem__.return_value
        coll.find.return_value.limit.return_value.max_time_ms.return_value = [{'_id': i} for i in range(101)]
        with patch.object(self.tools, 'mongo', return_value=mc), self.assertRaisesRegex(ValueError, '100'):
            self.tools.call('mongo_delete', {'namespace': 'demo.orders', 'filter': {'x': 1}})
        coll.delete_many.assert_not_called()

    def test_identity_is_checked_again_at_execution(self):
        with patch('api._assert_uri_targets', side_effect=api.HTTPException(409, 'other cluster')), patch.dict('os.environ', {'MONGODB_URI': 'configured'}), patch('api._mongo') as mongo:
            with self.assertRaises(api.HTTPException): self.tools.execute('mongo_drop_collection', {'namespace': 'demo.orders'})
            mongo.assert_not_called()

    def test_result_size_keeps_json_and_pagination(self):
        data = {'items': [{'v': 'x'*1000} for _ in range(20)], 'has_more': False, 'total_count': 20, 'next_offset': None}
        result = bounded_result(data, {'offset': 7}, budget=5000)
        self.assertTrue(result['has_more'])
        self.assertEqual(result['next_offset'], 7 + len(result['items']))
        self.assertLess(len(json.dumps(result)), 5000)
        with self.assertRaises(ValueError): bounded_result({'items': [{'v': 'x'*10000}]}, {}, budget=100)


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = ActionStore(Path(self.temp.name) / 'actions.db')
        self.payload = {'operation': 'mongo_delete', 'arguments': {'namespace': 'demo.orders', 'filter': {'x': 1}}, 'frozen_ids': [1], 'details': {'operation': 'Excluir', 'cluster': 'c'}, 'destructive': True}
        self.action = self.store.create('s'*32, 'p', 'c', self.payload)

    def tearDown(self): self.temp.cleanup()

    def test_parallel_approvals_execute_at_most_once(self):
        def claim(_): return self.store.claim(self.action['id'], 's'*32, 'p', 'c')[0]
        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(sum(pool.map(claim, range(12))), 1)

    def test_cross_session_and_cluster_are_rejected(self):
        for session, project, cluster in [('x'*32, 'p', 'c'), ('s'*32, 'other', 'c'), ('s'*32, 'p', 'other')]:
            with self.assertRaises(ValueError): self.store.claim(self.action['id'], session, project, cluster)

    def test_cancel_and_expiry_do_not_execute(self):
        execute, row = self.store.claim(self.action['id'], 's'*32, 'p', 'c', cancel=True)
        self.assertFalse(execute)
        self.assertEqual(row['state'], 'cancelled')
        self.assertFalse(self.store.claim(self.action['id'], 's'*32, 'p', 'c')[0])
        action = self.store.create('s'*32, 'p', 'c', self.payload)
        with self.store.connect() as db: db.execute('UPDATE actions SET expires=0 WHERE id=?', (action['id'],))
        self.assertFalse(self.store.claim(action['id'], 's'*32, 'p', 'c')[0])

    def test_restart_preserves_claim(self):
        self.store.claim(self.action['id'], 's'*32, 'p', 'c')
        restarted = ActionStore(self.store.path)
        self.assertFalse(restarted.claim(self.action['id'], 's'*32, 'p', 'c')[0])

    def test_api_rejects_modified_arguments_and_does_not_replay(self):
        body = {'session_id': 's'*32, 'project_id': 'p', 'cluster_name': 'c', 'decision': 'approve'}
        with patch.object(assistant_api, 'store', self.store), patch.object(api, '_API_AUTH_TOKEN', ''), patch('assistant_api.ClusterTools.execute', return_value={'deleted_count': 1}) as execute:
            client = TestClient(api.app)
            url = '/api/assistant/actions/' + self.action['id']
            self.assertEqual(client.post(url, json={**body, 'arguments': {}}).status_code, 422)
            self.assertEqual(client.post(url, json=body).json()['state'], 'completed')
            self.assertEqual(client.post(url, json=body).json()['state'], 'completed')
            execute.assert_called_once()

    def test_unknown_write_result_is_not_retried(self):
        body = assistant_api.DecisionBody(session_id='s'*32, project_id='p', cluster_name='c', decision='approve')
        with patch.object(assistant_api, 'store', self.store), patch('assistant_api.ClusterTools.execute', side_effect=TimeoutError) as execute:
            self.assertEqual(assistant_api.decide(self.action['id'], body)['state'], 'unknown')
            self.assertEqual(assistant_api.decide(self.action['id'], body)['state'], 'unknown')
            execute.assert_called_once()

    def test_auth_covers_assistant_and_actions(self):
        with patch.object(api, '_API_AUTH_TOKEN', 'secret'):
            client = TestClient(api.app)
            self.assertEqual(client.get('/api/assistant/capabilities').status_code, 401)
            self.assertEqual(client.post('/api/assistant/actions/list', json={}).status_code, 401)
            self.assertEqual(client.get('/api/assistant/capabilities', headers={'Authorization': 'Bearer secret'}).status_code, 200)


class MCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_stdio_handshake_catalog_and_read_only_call(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        params = StdioServerParameters(command=sys.executable, args=[str(Path(__file__).resolve().parents[1] / 'torre_mcp_server.py'), 'test', 'test'])
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listing = await session.list_tools()
                self.assertEqual({t.name for t in listing.tools}, set(TOOLS))
                result = await session.call_tool('atlas_cost', {'tier': 'M10'})
                self.assertFalse(result.isError)
                self.assertGreater(json.loads(result.content[0].text)['estimate']['usd'], 0)
                bad = await session.call_tool('mongo_delete', {'namespace': 'demo.orders', 'filter': {}, 'execute': True})
                self.assertTrue(bad.isError)



class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_loop_returns_proposal_without_executing_write(self):
        from contextlib import asynccontextmanager
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from anthropic.types import Message as ModelMessage
        from mcp.types import CallToolResult, TextContent, Tool
        import assistant_runtime
        proposal = {'proposal': True, 'operation': 'mongo_create_index', 'arguments': {'namespace': 'demo.orders', 'keys': [{'status': 1}]}, 'details': {'operation': 'Criar índice', 'cluster': 'c'}, 'frozen_ids': None, 'destructive': False}
        messages = [ModelMessage.model_validate({'id': 'm1', 'type': 'message', 'role': 'assistant', 'model': 'test', 'content': [{'type': 'tool_use', 'id': 't1', 'name': 'mongo_create_index', 'input': proposal['arguments']}], 'stop_reason': 'tool_use', 'usage': {'input_tokens': 1, 'output_tokens': 1}}),
                    ModelMessage.model_validate({'id': 'm2', 'type': 'message', 'role': 'assistant', 'model': 'test', 'content': [{'type': 'text', 'text': 'Ação preparada; aprove no cartão.'}], 'stop_reason': 'end_turn', 'usage': {'input_tokens': 1, 'output_tokens': 1}})]
        class Stream:
            def __init__(self, message): self.message = message
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            @property
            def text_stream(self):
                async def chunks():
                    for b in self.message.content:
                        if b.type == 'text': yield b.text
                return chunks()
            async def get_final_message(self): return self.message
        mcp = AsyncMock()
        mcp.list_tools.return_value = SimpleNamespace(tools=[Tool(name='mongo_create_index', description='prepare', inputSchema=TOOLS['mongo_create_index']['input_schema'])])
        mcp.call_tool.return_value = CallToolResult(content=[TextContent(type='text', text=json.dumps(proposal))])
        @asynccontextmanager
        async def stdio(*args): yield (None, None)
        @asynccontextmanager
        async def session(*args, **kwargs): yield mcp
        client = MagicMock()
        client.messages.stream.side_effect = [Stream(m) for m in messages]
        with tempfile.TemporaryDirectory() as directory:
            local_store = ActionStore(Path(directory) / 'actions.db')
            with patch.object(assistant_runtime, 'stdio_client', stdio), patch.object(assistant_runtime, 'ClientSession', session), patch.object(assistant_runtime, 'store', local_store), patch.object(ClusterTools, 'execute') as execute:
                events = [e async for e in assistant_runtime.run_assistant([{'role': 'user', 'content': 'Crie o índice'}], 'p', 'c', 's'*32, client=client)]
                execute.assert_not_called()
                actions = [e for e in events if e['type'] == 'action']
                self.assertEqual(len(actions), 1)
                self.assertEqual(actions[0]['action']['state'], 'pending')
                self.assertEqual(events[-1]['type'], 'done')
                tool_result = client.messages.stream.call_args.kwargs['messages'][2]['content'][0]
                self.assertIn('pending_user_approval', tool_result['content'])
                self.assertNotIn(actions[0]['action']['id'], tool_result['content'])

    async def test_closing_generator_cleans_up_real_mcp_transport(self):
        import assistant_runtime
        with tempfile.TemporaryDirectory() as directory, patch.object(assistant_runtime, 'store', ActionStore(Path(directory) / 'actions.db')):
            stream = assistant_runtime.run_assistant([{'role': 'user', 'content': 'oi'}], 'p', 'c', 's'*32, client=MagicMock())
            self.assertEqual((await anext(stream))['type'], 'connected')
            await stream.aclose()

if __name__ == '__main__': unittest.main()
