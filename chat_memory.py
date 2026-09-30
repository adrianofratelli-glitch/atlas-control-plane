"""
chat_memory.py — Chat history persisted in MongoDB Atlas
Showcases: Document Model · Embedded Arrays · Text Index · Aggregation

Collection: torre.chat_history
Structure:
{
  "_id": ObjectId,
  "cluster":    "inter",
  "title":      "Quais índices estão sendo sugeridos...",
  "messages": [
    { "role": "user",      "content": "...", "elapsed_ms": 0,    "ts": ISODate },
    { "role": "assistant", "content": "...", "elapsed_ms": 4100, "ts": ISODate }
  ],
  "created_at": ISODate,
  "updated_at": ISODate
}
"""

from datetime import datetime, timezone
import os
from threading import Lock
from typing import List, Dict, Optional
from bson import ObjectId
from pymongo import MongoClient, TEXT, DESCENDING
from pymongo.collection import Collection

DB_NAME   = "torre"
COLL_NAME = "chat_history"
CHAT_MAX_MESSAGES = max(2, int(os.getenv("CHAT_MAX_MESSAGES", "100")))
# TTL is mandatory — an unset or explicitly-zero CHAT_RETENTION_DAYS must not
# mean "keep forever" (unbounded collection growth); it falls back to a sane
# ceiling instead.
CHAT_RETENTION_DAYS_DEFAULT = 90
_retention_raw = os.getenv("CHAT_RETENTION_DAYS", "")
try:
    CHAT_RETENTION_DAYS = int(_retention_raw) if _retention_raw.strip() else CHAT_RETENTION_DAYS_DEFAULT
except ValueError:
    CHAT_RETENTION_DAYS = CHAT_RETENTION_DAYS_DEFAULT
if CHAT_RETENTION_DAYS <= 0:
    CHAT_RETENTION_DAYS = CHAT_RETENTION_DAYS_DEFAULT

_clients: dict = {}
_clients_lock = Lock()


def _oid(conversation_id: str) -> ObjectId:
    """Validates conversation_id up front — an invalid id must surface as a
    caller error (400), not an unhandled bson.errors.InvalidId (500)."""
    try:
        return ObjectId(conversation_id)
    except Exception as e:
        raise ValueError(f"invalid conversation_id: {conversation_id!r}") from e


# ── Connection ────────────────────────────────────────────────────────────────
def _get_collection(mongo_uri: str) -> Collection:
    with _clients_lock:
        if mongo_uri not in _clients:
            _clients[mongo_uri] = MongoClient(mongo_uri, serverSelectionTimeoutMS=5000)
    return _clients[mongo_uri][DB_NAME][COLL_NAME]


def init_db(mongo_uri: str):
    """Ensure indexes exist on the collection. Idempotent."""
    coll = _get_collection(mongo_uri)

    existing = {idx["name"] for idx in coll.list_indexes()}

    # Text index for simple semantic search over messages and title
    if "text_search" not in existing:
        coll.create_index(
            [("title", TEXT), ("messages.content", TEXT)],
            name="text_search",
            default_language="portuguese",
        )

    # Index for recent listing (sorted by updated_at)
    if "updated_at_desc" not in existing:
        coll.create_index([("updated_at", DESCENDING)], name="updated_at_desc")

    # Index by cluster (context filter)
    if "cluster_idx" not in existing:
        coll.create_index([("cluster", DESCENDING)], name="cluster_idx")

    # Composite index covering list_conversations' actual query shape:
    # $match on cluster + $sort by updated_at desc, in one index instead of
    # two separate single-field indexes.
    if "cluster_updated_at_idx" not in existing:
        coll.create_index(
            [("cluster", 1), ("updated_at", DESCENDING)],
            name="cluster_updated_at_idx",
        )

    # TTL is always active (see CHAT_RETENTION_DAYS default above) — the
    # collection must never grow unbounded even if the env var is unset/0.
    if "updated_at_ttl" not in existing:
        coll.create_index(
            [("updated_at", 1)],
            name="updated_at_ttl",
            expireAfterSeconds=CHAT_RETENTION_DAYS * 86400,
        )


# ── CRUD ──────────────────────────────────────────────────────────────────────
def new_conversation(mongo_uri: str, cluster: str = "", project_id: str = "") -> str:
    """Create a new conversation. Returns the _id as a string."""
    now  = datetime.now(timezone.utc)
    doc  = {
        "cluster":    cluster,
        "project_id": project_id,
        "title":      "Nova Conversa",
        "messages":   [],
        "created_at": now,
        "updated_at": now,
    }
    result = _get_collection(mongo_uri).insert_one(doc)
    return str(result.inserted_id)


def add_message(mongo_uri: str, conversation_id: str, role: str, content: str, elapsed_ms: int = 0):
    """
    $push the message into the embedded array and $set updated_at.
    Demonstrates the power of the Document Model — no separate messages table.
    """
    now = datetime.now(timezone.utc)
    msg = {"role": role, "content": content, "elapsed_ms": elapsed_ms, "ts": now}

    update = {
        "$push": {"messages": {"$each": [msg], "$slice": -CHAT_MAX_MESSAGES}},
        "$set":  {"updated_at": now},
    }

    # Auto-title: uses the user's first message
    if role == "user":
        title = content[:70] + ("…" if len(content) > 70 else "")
        # Only set the title if it is still "Nova Conversa"
        _get_collection(mongo_uri).update_one(
            {"_id": _oid(conversation_id), "title": "Nova Conversa"},
            {"$set": {"title": title}},
        )

    _get_collection(mongo_uri).update_one(
        {"_id": _oid(conversation_id)},
        update,
    )


def load_messages(mongo_uri: str, conversation_id: str) -> List[Dict]:
    """Return a conversation's messages. Embedded array = a single read."""
    doc = _get_collection(mongo_uri).find_one(
        {"_id": _oid(conversation_id)},
        {"messages": 1, "cluster": 1},
    )
    if not doc:
        return []
    return doc.get("messages", [])


def list_conversations(mongo_uri: str, cluster: str = "", limit: int = 25) -> List[Dict]:
    """
    List recent conversations with message count via $size.
    The aggregation pipeline demonstrates MongoDB's power for analytics.
    """
    coll     = _get_collection(mongo_uri)
    match    = {"cluster": cluster} if cluster else {}
    pipeline = [
        {"$match": match},
        {"$project": {
            "cluster":    1,
            "project_id": 1,
            "title":      1,
            "updated_at": 1,
            "created_at": 1,
            "msg_count":  {"$size": "$messages"},
        }},
        {"$sort":  {"updated_at": -1}},
        {"$limit": limit},
    ]
    return [
        {**doc, "id": str(doc["_id"])}
        for doc in coll.aggregate(pipeline)
    ]


def search_conversations(mongo_uri: str, query: str, limit: int = 8) -> List[Dict]:
    """
    Full-text search via MongoDB's text index.
    Demonstrates Atlas Text Search without Elasticsearch.
    """
    coll = _get_collection(mongo_uri)
    docs = coll.find(
        {"$text": {"$search": query}},
        {"title": 1, "cluster": 1, "project_id": 1, "updated_at": 1,
         "score": {"$meta": "textScore"},
         "msg_count": {"$size": "$messages"}},
    ).sort([("score", {"$meta": "textScore"})]).limit(limit)
    return [{**doc, "id": str(doc["_id"])} for doc in docs]


def delete_conversation(mongo_uri: str, conversation_id: str):
    """Delete a conversation by ID."""
    _get_collection(mongo_uri).delete_one({"_id": _oid(conversation_id)})


# ── Utilities ─────────────────────────────────────────────────────────────────
def format_relative_time(dt) -> str:
    """Convert a datetime to relative text (e.g. 'há 2h')."""
    try:
        if isinstance(dt, str):
            dt = datetime.fromisoformat(dt)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - dt
        s     = int(delta.total_seconds())
        if s < 60:     return "agora"
        if s < 3600:   return f"há {s//60}min"
        if s < 86400:  return f"há {s//3600}h"
        if s < 604800: return f"há {s//86400}d"
        return dt.strftime("%d/%m/%Y")
    except Exception:
        return "—"
