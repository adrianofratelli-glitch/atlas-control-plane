"""Operational assistant API. Shares the existing app's authentication middleware."""
from contextlib import aclosing
import asyncio
import json
import logging
import os
from typing import Literal
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from assistant_actions import store
from assistant_tools import ClusterTools, TOOLS, serializable
from assistant_runtime import run_assistant, encode
from llm_gateway import GatewayNotConfigured

router = APIRouter(prefix="/api/assistant")
logger = logging.getLogger("torre.assistant")


class SessionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(min_length=32, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    project_id: str = Field(default="", max_length=64, pattern=r"^[A-Za-z0-9]*$")
    cluster_name: str = Field(default="", max_length=64, pattern=r"^(?:[A-Za-z0-9][A-Za-z0-9-]*)?$")


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=12000)


class AssistantBody(SessionBody):
    messages: list[Message] = Field(min_length=1, max_length=16)
    conversation_id: str | None = None
    mode: Literal["chat", "report"] = "chat"


class DecisionBody(SessionBody):
    decision: Literal["approve", "cancel"]


@router.get("/capabilities")
def capabilities():
    return {"transport": "MCP · stdio", "tools": [{"name": t["name"], "label": t["label"], "approval": t["write"]} for t in TOOLS.values()],
            "data_connection_configured": bool(os.getenv("MONGODB_URI")),
            "limits": {"documents_per_write": 100, "page_size": 100, "approval_ttl_seconds": 900}}


def save_user(body):
    uri = os.getenv("MONGODB_URI", "")
    if not uri:
        return None
    import api
    from chat_memory import _get_collection, _oid, new_conversation, add_message
    if body.conversation_id:
        doc = _get_collection(uri).find_one({"_id": _oid(body.conversation_id)}, {"project_id": 1, "cluster": 1})
        if not doc or doc.get("project_id") != body.project_id or doc.get("cluster", "") != body.cluster_name:
            raise HTTPException(409, "Histórico pertence a outro contexto ou é antigo. Inicie uma nova conversa para operar este cluster.")
    api._ensure_chat_db(uri)
    conv_id = body.conversation_id or new_conversation(uri, body.cluster_name, body.project_id)
    add_message(uri, conv_id, "user", body.messages[-1].content)
    return conv_id


@router.post("")
async def assistant(body: AssistantBody):
    if body.messages[-1].role != "user":
        raise HTTPException(422, "A última mensagem deve ser do usuário.")
    if bool(body.project_id) != bool(body.cluster_name):
        raise HTTPException(422, "Informe projeto e cluster juntos.")
    conv_id = None
    try:
        if body.mode == "chat":
            conv_id = await asyncio.wait_for(asyncio.to_thread(save_user, body), timeout=8)
    except HTTPException:
        raise
    except Exception:
        logger.warning("Histórico indisponível; conversa segue sem persistência.")

    async def events():
        text = []
        completed = False
        if conv_id:
            yield encode({"type": "conversation", "id": conv_id})
        try:
            async with asyncio.timeout(240):
                async with aclosing(run_assistant([m.model_dump() for m in body.messages], body.project_id, body.cluster_name, body.session_id, mode=body.mode)) as stream:
                    async for event in stream:
                        if event["type"] == "text":
                            text.append(event["text"])
                        completed = completed or event["type"] == "done"
                        yield encode(event)
            if not completed:
                # Logical model limits emit an error; still terminate NDJSON cleanly.
                yield encode({"type": "done"})
        except asyncio.CancelledError:
            raise
        except GatewayNotConfigured as exc:
            yield encode({"type": "error", "message": exc.public_message})
            yield encode({"type": "done"})
        except TimeoutError:
            yield encode({"type": "error", "message": "Prazo de análise excedido. As evidências disponíveis continuam nos painéis; tente novamente."})
            yield encode({"type": "done"})
        except Exception:
            logger.warning("Rodada do assistente interrompida (MCP/modelo indisponível ou prazo excedido).")
            yield encode({"type": "error", "message": "Não foi possível concluir a conversa. Verifique a conexão MCP e a configuração do modelo. Ações pendentes podem ser recuperadas em Atualizar ações."})
            yield encode({"type": "done"})
        finally:
            if conv_id and text:
                try:
                    from chat_memory import add_message
                    await asyncio.to_thread(add_message, os.getenv("MONGODB_URI", ""), conv_id, "assistant", "".join(text))
                except Exception:
                    logger.warning("Não foi possível salvar resposta no histórico.")
    return StreamingResponse(events(), media_type="application/x-ndjson", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@router.post("/actions/list")
def actions(body: SessionBody):
    return {"actions": store.recent(body.session_id, body.project_id, body.cluster_name)}


@router.post("/actions/{action_id}")
def decide(action_id: str, body: DecisionBody):
    try:
        execute, row = store.claim(action_id, body.session_id, body.project_id, body.cluster_name, cancel=body.decision == "cancel")
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    if execute:
        payload = json.loads(row["payload"])
        try:
            result = ClusterTools(row["project"], row["cluster"]).execute(payload["operation"], payload["arguments"], payload.get("frozen_ids"))
            store.finish(action_id, "completed", serializable(result))
            # The legacy summary cache must not outlive an approved operation.
            import api
            api._chat_snapshots.pop((row["project"], row["cluster"]), None)
        except Exception:
            # A write timeout may have taken effect. Never replay automatically.
            store.finish(action_id, "unknown", {"message": "Não foi possível confirmar o resultado. Pode ter ocorrido alteração parcial. Consulte os dados/estado antes de preparar outra ação; esta ação não será reenviada."})
            logger.warning("Resultado de ação incerto id=%s operation=%s", action_id, payload["operation"])
    return store.public(store.get(action_id, body.session_id, body.project_id, body.cluster_name))
