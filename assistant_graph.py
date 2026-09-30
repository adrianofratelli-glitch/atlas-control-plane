"""O loop de ferramentas do assistente (Claude + MCP) como um StateGraph do LangGraph.

Antes era um `for _ in range(12)` dentro de `assistant_runtime._produce`, chamando o
Anthropic em streaming e depois as tools MCP, num loop reason-act clássico. Isto
reorganiza o MESMO loop em dois nós (`call_model` / `call_tools`) que se alternam até
o modelo não pedir mais ferramenta, esgotar as 12 rodadas ou estourar as 40 chamadas —
os mesmos três limites de antes, sem mudança de comportamento.

`_produce` continua dono da sessão MCP, do client Anthropic e do trace do Langfuse
(coisas ligadas ao `async with` do transporte stdio, que não fazem sentido dentro de um
checkpoint) — eles entram nos nós via `config["configurable"]`, nunca no estado do grafo.
"""

from __future__ import annotations

import json
import os
import time
from typing import Optional, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.mongodb import MongoDBSaver
from langgraph.graph import END, StateGraph
from pymongo import MongoClient as SyncMongoClient

import tracing
from ai_agent import MODEL, _track_usage
from assistant_tools import TOOLS

MAX_ITERATIONS = 12
MAX_TOOL_CALLS = 40


class LoopState(TypedDict, total=False):
    history: list
    system: str
    calls: int
    iteration: int
    full_response: str
    tool_uses: list
    outcome: Optional[dict]  # {"type": "done"} | {"type": "error", "message": ...}


def _ctx(config):
    return config["configurable"]


async def n_call_model(state: LoopState, config) -> dict:
    ctx = _ctx(config)
    client, emit, lf_trace = ctx["client"], ctx["emit"], ctx["lf_trace"]
    history = state["history"]
    iteration = state.get("iteration", 0) + 1
    if iteration > MAX_ITERATIONS:
        return {"outcome": {"type": "error",
                             "message": "Análise atingiu o limite de etapas. Peça para continuar; resultados podem estar parciais."}}

    iter_model = os.getenv("CLAUDE_MODEL", MODEL)
    iter_text = ""
    iter_t0 = time.perf_counter()
    async with client.messages.stream(model=iter_model, max_tokens=4096,
                                       system=state["system"], messages=history, tools=ctx["tools"]) as stream:
        async for text in stream.text_stream:
            iter_text += text
            await emit({"type": "text", "text": text})
        message = await stream.get_final_message()
        _track_usage(getattr(message, "usage", None))
    usage = getattr(message, "usage", None)
    tracing.log_generation(
        lf_trace, name="assistant.reasoning", model=iter_model,
        input_text=None, output_text=iter_text,
        usage={
            "input_tokens": getattr(usage, "input_tokens", 0) or 0,
            "output_tokens": getattr(usage, "output_tokens", 0) or 0,
            "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
            "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", 0) or 0,
        },
        latency_ms=int((time.perf_counter() - iter_t0) * 1000),
    )
    blocks = [b.model_dump(exclude_none=True) for b in message.content]
    history = history + [{"role": "assistant", "content": blocks}]
    tool_uses = [{"id": b.id, "name": b.name, "input": b.input}
                 for b in message.content if b.type == "tool_use"]

    if not tool_uses:
        outcome = {"type": "done"}
        if message.stop_reason == "max_tokens":
            outcome = {"type": "error",
                       "message": "Resposta atingiu o limite de tamanho. Peça para continuar; a resposta está parcial."}
        return {"history": history, "iteration": iteration,
                "full_response": state["full_response"] + iter_text, "outcome": outcome}

    return {"history": history, "iteration": iteration, "tool_uses": tool_uses,
            "full_response": state["full_response"] + iter_text}


def _route_after_model(state: LoopState) -> str:
    return "done" if state.get("outcome") else "call_tools"


def _route_after_tools(state: LoopState) -> str:
    return "done" if state.get("outcome") else "call_model"


async def n_call_tools(state: LoopState, config) -> dict:
    ctx = _ctx(config)
    mcp, emit, lf_trace = ctx["mcp"], ctx["emit"], ctx["lf_trace"]
    store = ctx["store"]
    session_id, project_id, cluster_name = ctx["session_id"], ctx["project_id"], ctx["cluster_name"]
    calls = state.get("calls", 0)
    results = []
    for call in state["tool_uses"]:
        calls += 1
        if calls > MAX_TOOL_CALLS:
            return {"calls": calls, "outcome": {
                "type": "error", "message": "Limite de 40 consultas por rodada atingido. Peça para continuar a análise."}}
        label = TOOLS.get(call["name"], {}).get("label", call["name"])
        await emit({"type": "tool_start", "id": call["id"], "label": label})
        tool_t0 = time.perf_counter()
        response = await mcp.call_tool(call["name"], call["input"])
        content = "\n".join(b.text for b in response.content if b.type == "text")
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            if not response.isError:
                raise ValueError("Ferramenta retornou uma resposta inválida.")
            data = {"error": "Argumentos inválidos ou ferramenta indisponível. Confira o schema e corrija a chamada."}
            content = json.dumps(data, ensure_ascii=False)
        if not response.isError and TOOLS.get(call["name"], {}).get("write") and data.get("proposal") is True:
            if data.get("operation") != call["name"] or data.get("arguments") != call["input"]:
                raise ValueError("Prévia MCP diverge da operação solicitada.")
            import asyncio
            action = await asyncio.to_thread(store.create, session_id, project_id, cluster_name, data)
            await emit({"type": "action", "action": action})
            content = json.dumps({"status": "pending_user_approval", "details": data["details"]}, ensure_ascii=False)
        tracing.log_span(
            lf_trace, name=f"tool.{call['name']}", input_data=call["input"], output_data=data,
            metadata={"is_error": response.isError, "latency_ms": int((time.perf_counter() - tool_t0) * 1000)})
        await emit({"type": "tool_end", "id": call["id"], "label": label, "ok": not response.isError,
                    "message": data.get("error") if response.isError else
                    "Consulta concluída" if not TOOLS.get(call["name"], {}).get("write") else "Ação preparada; aguardando aprovação"})
        results.append({"type": "tool_result", "tool_use_id": call["id"], "content": content, "is_error": response.isError})

    history = state["history"] + [{"role": "user", "content": results}]
    last_assistant_blocks = state["history"][-1]["content"]
    if any(block.get("type") == "text" and block.get("text") for block in last_assistant_blocks):
        await emit({"type": "text", "text": "\n\n"})
    return {"history": history, "calls": calls, "tool_uses": []}


_GRAPH = None
_CHECKPOINT_CLIENT: SyncMongoClient | None = None


def _build_graph():
    builder = StateGraph(LoopState)
    builder.add_node("call_model", n_call_model)
    builder.add_node("call_tools", n_call_tools)
    builder.set_entry_point("call_model")
    builder.add_conditional_edges("call_model", _route_after_model, {"done": END, "call_tools": "call_tools"})
    builder.add_conditional_edges("call_tools", _route_after_tools, {"done": END, "call_model": "call_model"})

    global _CHECKPOINT_CLIENT
    mongo_uri = os.getenv("MONGODB_URI", "")
    if mongo_uri:
        _CHECKPOINT_CLIENT = SyncMongoClient(mongo_uri)
        checkpointer = MongoDBSaver(
            _CHECKPOINT_CLIENT, db_name=os.getenv("MONGODB_DB", "torre"),
            checkpoint_collection_name="langgraph_checkpoints",
            writes_collection_name="langgraph_checkpoint_writes",
        )
    else:
        # MONGODB_URI é opcional nesta PoV (só histórico de chat + índices); sem ele
        # o checkpoint do turno vira em memória do processo — o loop continua real,
        # só não sobrevive a um restart do backend entre turnos.
        checkpointer = MemorySaver()
    return builder.compile(checkpointer=checkpointer)


def get_graph():
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = _build_graph()
    return _GRAPH


async def run_loop(*, history, system, tools, client, mcp, emit, lf_trace, store,
                    session_id, project_id, cluster_name) -> str:
    """Roda o loop reason-act até o fim e devolve o texto completo da resposta.

    `store` vem de quem chama (`assistant_runtime.store`, o nome do módulo, não um
    import próprio) — é o que os testes trocam por um `ActionStore` de teste via
    `patch.object(assistant_runtime, 'store', ...)`. Importar `store` direto aqui
    criaria uma segunda referência que o patch nunca alcança.
    """
    graph = get_graph()
    initial: LoopState = {"history": history, "system": system, "calls": 0,
                           "iteration": 0, "full_response": ""}
    config = {"configurable": {
        "client": client, "tools": tools, "mcp": mcp, "emit": emit, "lf_trace": lf_trace,
        "store": store, "session_id": session_id, "project_id": project_id,
        "cluster_name": cluster_name, "thread_id": session_id,
    }}
    final_state = await graph.ainvoke(initial, config=config)
    outcome = final_state.get("outcome") or {"type": "done"}
    await emit(outcome if outcome["type"] == "error" else {"type": "done"})
    return final_state.get("full_response", "")
