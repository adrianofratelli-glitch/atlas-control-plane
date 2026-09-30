"""
populate_workload.py (v3)
Populates Atlas Query Profiler + Performance Advisor + metrics (CPU/IOPS)
with a weighted, bounded workload sized for an M20 with 60+ GB of data.

Differences from populate_profiler.py:
  - Weighted query shapes: cheap ones run often, monster scans run rarely.
  - maxTimeMS on everything (nothing runs for minutes and clogs the queue).
  - Targets only fields still WITHOUT indexes, so Performance Advisor has
    fresh suggestions (segmento/account_number compounds already exist).
  - Dedicated write threads (inserts + updates) to move CPU/IOPS for the
    Scale and FinOps tabs.

Usage: python populate_workload.py [minutes]   (default 30)
"""

import os
import sys
import time
import random
import threading
from datetime import datetime, timezone

from pymongo import MongoClient
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

URI = os.getenv("MONGODB_URI")

READ_THREADS  = 6
WRITE_THREADS = 2
DURATION_SECS = (int(sys.argv[1]) if len(sys.argv) > 1 else 30) * 60
PRINT_EVERY   = 50

# ── DATA ──────────────────────────────────────────────────────────────
ACCOUNT_NUMBERS = [
    "2659850271393050232", "2375809432101401449",
    "2326393752287659295", "2287996907938235286",
    "2967547710284278035",
]
CATEGORY_CODES = ["5942", "5300", "5814", "5812", "5977", "5999", "5411", "5651"]
SEGMENTS       = ["s1", "s2", "s3", "s4"]
TX_TYPES       = ["d", "c"]
PLANS          = ["10002", "10003", "10006"]
PREFIXES       = ["SHOPEE", "AMAZON", "IFOOD", "SUPERMERCADO", "FORT", "UBER", "RAPPI", "MERCADO"]
DATES_TX       = [("20250101", "20250331"), ("20250401", "20250630"), ("20240101", "20241231")]
DATES_FAT      = [("20240101", "20240630"), ("20240701", "20241231"), ("20230101", "20231231")]

lock       = threading.Lock()
counter    = {"total": 0, "errors": 0, "timeouts": 0, "writes": 0}
stop_event = threading.Event()
START_TIME = time.time()


def inc(key="total"):
    with lock:
        counter[key] += 1
        if key == "total" and counter["total"] % PRINT_EVERY == 0:
            elapsed = int(time.time() - START_TIME)
            print(f"  [{counter['total']} reads | {counter['writes']} writes | "
                  f"{counter['timeouts']} timeouts | {counter['errors']} erros | {elapsed}s]")


def run(fn, params):
    try:
        result = fn(params)
        if hasattr(result, "__iter__"):
            list(result)
        inc("total")
    except Exception as e:
        if "MaxTimeMS" in str(e) or "operation exceeded time limit" in str(e).lower():
            inc("timeouts")
            inc("total")  # a timed-out op still lands in the slow query log
        else:
            inc("errors")


# ── READ SHAPES ───────────────────────────────────────────────────────
# (weight, fn) — weight is relative frequency. Heavy scans get low weight
# so they show up in the Profiler without monopolizing the M20.

def make_shapes(db):
    tx  = db["transacoes"]
    fat = db["fatura"]

    def p():
        return dict(
            seg=random.choice(SEGMENTS), cat=random.choice(CATEGORY_CODES),
            acc=random.choice(ACCOUNT_NUMBERS), typ=random.choice(TX_TYPES),
            plan=random.choice(PLANS), inst=str(random.randint(1, 12)),
            prefix=random.choice(PREFIXES),
            amt_lo=str(random.randint(50, 1500)), amt_hi=str(random.randint(2000, 9000)),
            d_tx=random.choice(DATES_TX), d_fat=random.choice(DATES_FAT),
        )

    shapes = [
        # -- fatura: no useful index on any of these fields → COLLSCAN / PA gold
        (10, lambda v: fat.find({"amss_mt_type": v["typ"]}).limit(200).max_time_ms(15000)),
        (10, lambda v: fat.find({"amss_mt_category_code": v["cat"]}).limit(150).max_time_ms(15000)),
        (8,  lambda v: fat.find({"amss_mt_plan": v["plan"], "amss_mt_type": v["typ"]}).limit(100).max_time_ms(15000)),
        (8,  lambda v: fat.find({"amss_mt_eff_date": {"$gte": v["d_fat"][0], "$lte": v["d_fat"][1]}}).limit(100).max_time_ms(15000)),
        (6,  lambda v: fat.find({"amss_mt_amount": {"$gt": v["amt_lo"], "$lt": v["amt_hi"]}}).limit(100).max_time_ms(15000)),
        (5,  lambda v: fat.find({"amss_mt_desc": {"$regex": f"^{v['prefix']}", "$options": "i"}}).limit(30).max_time_ms(20000)),
        # fatura: indexed account_number but blocking sort on unindexed field
        (8,  lambda v: fat.find({"account_number": v["acc"]}).sort("amss_mt_eff_date", -1).limit(50).max_time_ms(15000)),
        # fatura: IXSCAN on segmento + residual filter → PA suggests compound
        (6,  lambda v: fat.find({"segmento": v["seg"], "amss_mt_category_code": v["cat"]}).limit(100).max_time_ms(20000)),

        # -- transacoes: fields still without indexes
        (10, lambda v: tx.find({"amos_mt_type": v["typ"]}).limit(200).max_time_ms(15000)),
        (8,  lambda v: tx.find({"amos_mt_plan": v["plan"], "amos_mt_type": v["typ"]}).limit(100).max_time_ms(15000)),
        (8,  lambda v: tx.find({"amos_mt_eff_date": {"$gte": v["d_tx"][0], "$lte": v["d_tx"][1]}}).limit(100).max_time_ms(15000)),
        (6,  lambda v: tx.find({"amos_mt_amount": {"$gt": v["amt_lo"], "$lt": v["amt_hi"]}}).limit(100).max_time_ms(15000)),
        (6,  lambda v: tx.find({"amos_mt_inst_nbr": v["inst"], "amos_mt_plan": v["plan"]}).limit(80).max_time_ms(15000)),
        # case-insensitive regex can't use the amos_mt_desc index bounds
        (5,  lambda v: tx.find({"amos_mt_desc": {"$regex": f"^{v['prefix']}", "$options": "i"}}).limit(30).max_time_ms(20000)),
        # IXSCAN on segmento + blocking sort on unindexed eff_date
        (5,  lambda v: tx.find({"segmento": v["seg"], "amos_mt_type": v["typ"]}).sort("amos_mt_eff_date", -1).limit(50).max_time_ms(25000)),

        # -- heavy aggregations (rare, bounded) → dramatic Profiler entries
        (2,  lambda v: tx.aggregate([
                {"$match": {"account_number": v["acc"]}},
                {"$group": {"_id": "$amos_mt_category_code",
                            "total": {"$sum": {"$toDouble": "$amos_mt_amount"}},
                            "qtd":   {"$sum": 1},
                            "media": {"$avg": {"$toDouble": "$amos_mt_amount"}}}},
                {"$sort": {"total": -1}}, {"$limit": 5},
             ], maxTimeMS=30000)),
        (1,  lambda v: fat.aggregate([
                {"$match": {"account_number": v["acc"], "amss_mt_type": v["typ"]}},
                {"$group": {"_id": "$amss_mt_category_code",
                            "total": {"$sum": {"$toDouble": "$amss_mt_amount"}},
                            "count": {"$sum": 1}}},
                {"$sort": {"total": -1}}, {"$limit": 10},
             ], maxTimeMS=30000)),
        (1,  lambda v: tx.aggregate([
                {"$match": {"segmento": v["seg"], "amos_mt_type": v["typ"]}},
                {"$group": {"_id": "$amos_mt_category_code",
                            "total": {"$sum": {"$toDouble": "$amos_mt_amount"}},
                            "count": {"$sum": 1}}},
                {"$sort": {"total": -1}}, {"$limit": 10},
             ], maxTimeMS=45000)),
        # monster count (very rare) — huge docsExamined for the Profiler story
        (1,  lambda v: tx.count_documents({"segmento": v["seg"], "amos_mt_type": v["typ"]}, maxTimeMS=45000)),
    ]
    weights = [w for w, _ in shapes]
    fns     = [f for _, f in shapes]
    return weights, fns, p


def read_worker():
    cli = MongoClient(URI, serverSelectionTimeoutMS=10000)
    db = cli["banco_inter"]
    weights, fns, p = make_shapes(db)
    while not stop_event.is_set():
        fn = random.choices(fns, weights=weights, k=1)[0]
        run(fn, p())
    cli.close()


# ── WRITE WORKER ──────────────────────────────────────────────────────
def write_worker():
    cli = MongoClient(URI, serverSelectionTimeoutMS=10000)
    ev = cli["banco_inter"]["eventos_pix"]
    while not stop_event.is_set():
        try:
            docs = [{
                "account_number": random.choice(ACCOUNT_NUMBERS),
                "tipo": random.choice(["pix_in", "pix_out", "ted", "boleto"]),
                "valor": round(random.uniform(5, 5000), 2),
                "segmento": random.choice(SEGMENTS),
                "status": "pendente",
                "ts": datetime.now(timezone.utc),
            } for _ in range(200)]
            ev.insert_many(docs)
            # update without index on status → scans, generates write + read load
            ev.update_many(
                {"status": "pendente", "valor": {"$lt": 1000}},
                {"$set": {"status": "liquidado"}},
            )
            with lock:
                counter["writes"] += 201
            time.sleep(0.5)
        except Exception:
            inc("errors")
    cli.close()


# ── MAIN ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"Workload: {READ_THREADS} read + {WRITE_THREADS} write threads, "
          f"{DURATION_SECS // 60} min. Início: {datetime.now().strftime('%H:%M:%S')}\n")

    threads = [threading.Thread(target=read_worker, daemon=True) for _ in range(READ_THREADS)]
    threads += [threading.Thread(target=write_worker, daemon=True) for _ in range(WRITE_THREADS)]
    for t in threads:
        t.start()

    try:
        time.sleep(DURATION_SECS)
    except KeyboardInterrupt:
        print("\n[interrompido]")

    stop_event.set()
    for t in threads:
        t.join(timeout=15)

    elapsed = time.time() - START_TIME
    print(f"\nFim: {datetime.now().strftime('%H:%M:%S')} | {int(elapsed)}s")
    print(f"Reads: {counter['total']} ({counter['timeouts']} timeouts) | "
          f"Writes: {counter['writes']} | Erros: {counter['errors']}")
    print(f"Throughput: {counter['total'] / elapsed:.1f} reads/s")
