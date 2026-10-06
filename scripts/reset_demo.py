"""Reset idempotente do estado da demo Torre.

O que a Torre grava (e este script recria do zero):
  1. <db>.chat_history            histórico do chat (recriado vazio, com os 5 índices + TTL)
  2. <db>.langgraph_checkpoints    checkpoints do loop do assistente (apagados)
  3. <db>.langgraph_checkpoint_writes
  4. aprovações locais em .assistant-state/actions.sqlite3 (ou TORRE_ACTION_DB)
  5. documentos marcados `_torre_workload: true` que populate_workload.py inseriu em
     <workload-db>.eventos_pix (só os marcados; nada mais do banco é tocado)

O que NÃO é dado da Torre e por isso não é recriado: o estado da Atlas Admin API (clusters,
métricas, Performance Advisor, Profiler) e o dataset `banco_inter.transacoes/fatura`, que é
pré-existente no cluster e só é lido.

Guarda: recusa o banco da demo (MONGODB_DB, padrão "torre") e o workload-db da demo
("banco_inter") sem ALLOW_DEMO_DB_WRITE=1. Bancos terminados em `_test` são sempre aceitos.

    venv/bin/python scripts/reset_demo.py --db torre_test --workload-db banco_inter_test   # teste
    ALLOW_DEMO_DB_WRITE=1 venv/bin/python scripts/reset_demo.py                             # demo
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

CHECKPOINT_COLLECTIONS = ("langgraph_checkpoints", "langgraph_checkpoint_writes")
WORKLOAD_COLLECTION = "eventos_pix"
WORKLOAD_MARKER = "_torre_workload"
DEMO_WORKLOAD_DB = "banco_inter"


def demo_db_name() -> str:
    return os.getenv("MONGODB_DB", "torre")


def assert_writable(db_name: str, *, demo_names: set[str]) -> None:
    if db_name.endswith("_test"):
        return
    if db_name in demo_names and os.getenv("ALLOW_DEMO_DB_WRITE") != "1":
        raise SystemExit(f"Recusado: '{db_name}' é o banco da demo. Use um banco *_test ou "
                         "exporte ALLOW_DEMO_DB_WRITE=1 para resetar a demo de propósito.")
    if db_name not in demo_names:
        raise SystemExit(f"Recusado: '{db_name}' não é o banco da demo nem termina em _test.")


def reset(uri: str, db_name: str, workload_db: str, action_db: Path) -> dict:
    from pymongo import MongoClient
    import chat_memory

    client = MongoClient(uri, serverSelectionTimeoutMS=10000)
    db = client[db_name]
    summary = {"db": db_name}
    summary["chat_history_removidos"] = db[chat_memory.COLL_NAME].estimated_document_count()
    db.drop_collection(chat_memory.COLL_NAME)
    chat_memory.init_db(uri, db_name)
    for name in CHECKPOINT_COLLECTIONS:
        summary[f"{name}_removidos"] = db[name].estimated_document_count()
        db.drop_collection(name)
    summary["workload_marcados_removidos"] = client[workload_db][WORKLOAD_COLLECTION].delete_many(
        {WORKLOAD_MARKER: True}).deleted_count

    removed = 0
    if action_db.exists():
        with sqlite3.connect(action_db) as conn:
            exists = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='actions'").fetchone()
            if exists:
                removed = conn.execute("DELETE FROM actions").rowcount
    summary["acoes_locais_removidas"] = removed

    # Verificação: estado final esperado.
    indexes = {i["name"] for i in db[chat_memory.COLL_NAME].list_indexes()}
    expected = {"_id_", "text_search", "updated_at_desc", "cluster_idx", "cluster_updated_at_idx", "updated_at_ttl"}
    missing = expected - indexes
    if missing or db[chat_memory.COLL_NAME].count_documents({}) or any(
            db[n].count_documents({}) for n in CHECKPOINT_COLLECTIONS):
        raise SystemExit(f"Verificação falhou: índices ausentes={sorted(missing)} ou coleções não vazias.")
    summary["indices_chat_history"] = sorted(indexes)
    client.close()
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=demo_db_name(), help="banco de estado da Torre (padrão: MONGODB_DB ou torre)")
    parser.add_argument("--workload-db", default=DEMO_WORKLOAD_DB, help="banco onde populate_workload.py escreve")
    parser.add_argument("--action-db", default=os.getenv("TORRE_ACTION_DB", str(ROOT / ".assistant-state" / "actions.sqlite3")))
    args = parser.parse_args(argv)
    uri = os.getenv("MONGODB_URI", "")
    if not uri:
        raise SystemExit("MONGODB_URI não definida no .env.")
    assert_writable(args.db, demo_names={demo_db_name(), "torre"})
    assert_writable(args.workload_db, demo_names={DEMO_WORKLOAD_DB})
    summary = reset(uri, args.db, args.workload_db, Path(args.action_db))
    for key, value in summary.items():
        print(f"{key}: {value}")
    print("Reset concluído.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
