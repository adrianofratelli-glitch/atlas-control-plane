"""Regressão offline do loop de ferramentas do assistente (assistant_graph.py).

Não existia suíte nenhuma cobrindo assistant_runtime.py antes desta migração — o loop
só era exercitado manualmente, contra Claude e MCP reais. Estes testes usam um client
Anthropic e uma sessão MCP falsos para provar os mesmos três limites do loop original
(sem tool_use encerra, 12 rodadas no máximo, 40 chamadas de tool no máximo) sem gastar
tokens nem precisar de um cluster Atlas.
"""
import asyncio
import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["MONGODB_URI"] = ""  # sem checkpoint real neste arquivo — MemorySaver, mesmo se o shell tiver a URI exportada

import assistant_graph  # noqa: E402


class FakeStream:
    def __init__(self, text_chunks, message):
        self._chunks = text_chunks
        self._message = message

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    @property
    async def text_stream(self):
        for chunk in self._chunks:
            yield chunk

    def __aiter__(self):
        return self.text_stream.__aiter__()

    async def get_final_message(self):
        return self._message


class FakeMessages:
    def __init__(self, script):
        self._script = list(script)

    def stream(self, **kwargs):
        chunks, message = self._script.pop(0)
        return FakeStream(chunks, message)


class FakeClient:
    def __init__(self, script):
        self.messages = FakeMessages(script)


def _text_block(text):
    return SimpleNamespace(type="text", text=text, model_dump=lambda exclude_none=True: {"type": "text", "text": text})


def _tool_block(id_, name, input_):
    return SimpleNamespace(type="tool_use", id=id_, name=name, input=input_,
                            model_dump=lambda exclude_none=True: {"type": "tool_use", "id": id_, "name": name, "input": input_})


def _message(content, stop_reason="end_turn"):
    return SimpleNamespace(content=content, stop_reason=stop_reason, usage=None)


class FakeToolResponse:
    def __init__(self, payload, is_error=False):
        self.content = [SimpleNamespace(type="text", text=json.dumps(payload))]
        self.isError = is_error


class FakeMCP:
    def __init__(self, responses):
        self._responses = responses
        self.calls = []

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        return self._responses.pop(0)


def _run(coro):
    return asyncio.run(coro)


class ToolLoopTests(unittest.TestCase):
    def setUp(self):
        assistant_graph._GRAPH = None  # cada teste recompila com o checkpointer certo (MONGODB_URI vazia -> MemorySaver)

    async def _events(self, script, mcp_responses, tools=None):
        events = []

        async def emit(event):
            events.append(event)

        client = FakeClient(script)
        mcp = FakeMCP(mcp_responses)
        response = await assistant_graph.run_loop(
            history=[{"role": "user", "content": "status do cluster?"}],
            system="system prompt", tools=tools or [{"name": "atlas_status", "description": "", "input_schema": {}}],
            client=client, mcp=mcp, emit=emit, lf_trace=None, store=SimpleNamespace(create=lambda *a, **k: None),
            session_id="sess-1", project_id="proj-1", cluster_name="cluster-1",
        )
        return response, events, mcp.calls

    def test_no_tool_use_ends_the_loop_immediately(self):
        script = [(["Tudo certo com o cluster."], _message([_text_block("Tudo certo com o cluster.")]))]
        response, events, calls = _run(self._events(script, []))
        self.assertEqual(response, "Tudo certo com o cluster.")
        self.assertEqual(calls, [])
        self.assertEqual(events[-1], {"type": "done"})

    def test_one_tool_round_trip_then_final_answer(self):
        script = [
            (["Vou checar. "], _message([_text_block("Vou checar. "), _tool_block("t1", "atlas_status", {})])),
            (["Está saudável."], _message([_text_block("Está saudável.")])),
        ]
        mcp_responses = [FakeToolResponse({"status": "ok"})]
        response, events, calls = _run(self._events(script, mcp_responses))
        self.assertEqual(response, "Vou checar. Está saudável.")
        self.assertEqual(calls, [("atlas_status", {})])
        tool_events = [e["type"] for e in events]
        self.assertIn("tool_start", tool_events)
        self.assertIn("tool_end", tool_events)
        self.assertEqual(events[-1], {"type": "done"})

    def test_iteration_cap_stops_the_loop_with_an_explicit_message(self):
        # 13 rodadas pedindo tool sempre: a 13ª nunca chama o modelo de novo.
        script = [(["pensando"], _message([_tool_block(f"t{i}", "atlas_status", {})]))
                  for i in range(13)]
        mcp_responses = [FakeToolResponse({"status": "ok"}) for _ in range(12)]
        response, events, calls = _run(self._events(script, mcp_responses))
        self.assertEqual(len(calls), 12)
        self.assertEqual(events[-1]["type"], "error")
        self.assertIn("limite de etapas", events[-1]["message"])

    def test_tool_call_cap_stops_mid_round_with_an_explicit_message(self):
        many_tools = [_tool_block(f"t{i}", "atlas_status", {}) for i in range(41)]
        script = [(["pensando"], _message(many_tools))]
        mcp_responses = [FakeToolResponse({"status": "ok"}) for _ in range(40)]
        response, events, calls = _run(self._events(script, mcp_responses))
        self.assertEqual(len(calls), 40)
        self.assertEqual(events[-1]["type"], "error")
        self.assertIn("40 consultas", events[-1]["message"])

    def test_max_tokens_stop_reason_reports_partial_answer(self):
        script = [(["resposta cortada"], _message([_text_block("resposta cortada")], stop_reason="max_tokens"))]
        response, events, calls = _run(self._events(script, []))
        self.assertEqual(calls, [])
        self.assertEqual(events[-1]["type"], "error")
        self.assertIn("limite de tamanho", events[-1]["message"])


if __name__ == "__main__":
    unittest.main()
