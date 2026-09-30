"""
populate_profiler_v2.py
Populates Atlas Performance Advisor + Query Profiler with a parallel workload.
Target: 20-30 minutes of varied queries to trigger index suggestions.
"""

import os
import time
import random
import threading
from pymongo import MongoClient
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

URI = os.getenv("MONGODB_URI", "mongodb+srv://<user>:<pass>@<cluster>.mongodb.net/<database>")

# ── CONFIG ────────────────────────────────────────────────────────────
NUM_THREADS   = 8      # parallel threads — M10/M20 handles this fine
DURATION_SECS = 25 * 60  # 25 minutes
PRINT_EVERY   = 100    # print every N queries

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

# ── COUNTERS (thread-safe) ────────────────────────────────────────────
lock    = threading.Lock()
counter = {"total": 0, "errors": 0}
stop_event = threading.Event()

def inc(success=True):
    with lock:
        if success:
            counter["total"] += 1
            if counter["total"] % PRINT_EVERY == 0:
                elapsed = time.time() - START_TIME
                print(f"  [{counter['total']} queries | {int(elapsed)}s | {counter['errors']} erros]")
        else:
            counter["errors"] += 1

def run(label, cursor_or_result):
    try:
        list(cursor_or_result)
        inc(True)
    except Exception as e:
        inc(False)

# ── WORKER ────────────────────────────────────────────────────────────
def worker():
    # each thread has its own client (thread-safe)
    cli = MongoClient(URI, serverSelectionTimeoutMS=10000)
    db  = cli["banco_inter"]
    fat = db["fatura"]
    tx  = db["transacoes"]

    while not stop_event.is_set():
        seg   = random.choice(SEGMENTS)
        cat   = random.choice(CATEGORY_CODES)
        acc   = random.choice(ACCOUNT_NUMBERS)
        typ   = random.choice(TX_TYPES)
        plan  = random.choice(PLANS)
        inst  = str(random.randint(1, 12))
        prefix = random.choice(PREFIXES)
        amt_lo = str(random.randint(50, 1500))
        amt_hi = str(random.randint(2000, 9000))
        d_tx   = random.choice(DATES_TX)
        d_fat  = random.choice(DATES_FAT)

        # 1. Full scan by type (no useful index)
        run("tx type",  tx.find({"amos_mt_type": typ}).limit(200))
        run("fat type", fat.find({"amss_mt_type": typ}).limit(200))

        # 2. Range on an unindexed field
        run("tx amt range",  tx.find({"amos_mt_amount": {"$gt": amt_lo, "$lt": amt_hi}}).limit(100))
        run("fat amt range", fat.find({"amss_mt_amount": {"$gt": amt_lo, "$lt": amt_hi}}).limit(100))

        # 3. Account + sort without a compound index → COLLSCAN + in-memory sort
        run("tx acc sort",  tx.find({"account_number": acc}).sort("amos_mt_amount", -1).limit(50))
        run("fat acc sort", fat.find({"account_number": acc}).sort("amss_mt_amount", -1).limit(50))

        # 4. Category + segment
        run("tx cat+seg",  tx.find({"amos_mt_category_code": cat, "segmento": seg}).limit(100))
        run("fat cat+seg", fat.find({"amss_mt_category_code": cat, "segmento": seg}).limit(100))

        # 5. Plan + type
        run("tx plan+type",  tx.find({"amos_mt_plan": plan, "amos_mt_type": typ}).limit(100))
        run("fat plan+type", fat.find({"amss_mt_plan": plan, "amss_mt_type": typ}).limit(100))

        # 6. Segment + sort (expensive in-memory sort)
        run("tx seg sort",  tx.find({"segmento": seg}).sort("amos_mt_amount", -1).limit(50))
        run("fat seg sort", fat.find({"segmento": seg}).sort("amss_mt_amount", -1).limit(50))

        # 7. Multi-field without a compound index
        run("tx multi",  tx.find({"segmento": seg, "amos_mt_type": typ, "amos_mt_plan": plan}).limit(50))
        run("fat multi", fat.find({"segmento": seg, "amss_mt_type": typ, "amss_mt_plan": plan}).limit(50))

        # 8. Date range
        run("tx date",  tx.find({"amos_mt_eff_date":  {"$gte": d_tx[0],  "$lte": d_tx[1]}}).limit(100))
        run("fat date", fat.find({"amss_mt_eff_date": {"$gte": d_fat[0], "$lte": d_fat[1]}}).limit(100))

        # 9. Installment + segment
        run("tx inst", tx.find({"amos_mt_inst_nbr": inst, "segmento": seg}).limit(80))

        # 10. Regex (very slow → great for the Profiler)
        run("tx regex",  tx.find({"amos_mt_desc":  {"$regex": f"^{prefix}", "$options": "i"}}).limit(30))
        run("fat regex", fat.find({"amss_mt_desc": {"$regex": f"^{prefix}", "$options": "i"}}).limit(30))

        # 11. Aggregation: category by segment
        run("tx agg cat", tx.aggregate([
            {"$match": {"segmento": seg, "amos_mt_type": typ}},
            {"$group": {"_id": "$amos_mt_category_code",
                        "total": {"$sum": {"$toDouble": "$amos_mt_amount"}},
                        "count": {"$sum": 1}}},
            {"$sort": {"total": -1}},
            {"$limit": 10}
        ]))

        run("fat agg cat", fat.aggregate([
            {"$match": {"segmento": seg, "amss_mt_type": typ}},
            {"$group": {"_id": "$amss_mt_category_code",
                        "total": {"$sum": {"$toDouble": "$amss_mt_amount"}},
                        "count": {"$sum": 1}}},
            {"$sort": {"total": -1}},
            {"$limit": 10}
        ]))

        # 12. Aggregation: profile by account
        run("tx agg acc", tx.aggregate([
            {"$match": {"account_number": acc}},
            {"$group": {"_id": "$amos_mt_category_code",
                        "total": {"$sum": {"$toDouble": "$amos_mt_amount"}},
                        "qtd":   {"$sum": 1},
                        "media": {"$avg": {"$toDouble": "$amos_mt_amount"}}}},
            {"$sort": {"total": -1}},
            {"$limit": 5}
        ]))

        # 13. Aggregation: top spenders
        run("tx agg top", tx.aggregate([
            {"$match": {"segmento": seg}},
            {"$group": {"_id": "$account_number",
                        "total": {"$sum": {"$toDouble": "$amos_mt_amount"}}}},
            {"$sort": {"total": -1}},
            {"$limit": 10}
        ]))

        # 14. Aggregation: daily volume
        run("tx agg diaria", tx.aggregate([
            {"$match": {"segmento": seg}},
            {"$group": {"_id": "$diaria_data",
                        "volume": {"$sum": {"$toDouble": "$amos_mt_amount"}},
                        "count":  {"$sum": 1}}},
            {"$sort": {"_id": -1}},
            {"$limit": 30}
        ]))

        # 15. count without an index
        run("tx count seg",  tx.count_documents({"segmento": seg, "amos_mt_type": typ}))
        run("fat count seg", fat.count_documents({"segmento": seg, "amss_mt_type": typ}))

    cli.close()


# ── MAIN ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    START_TIME = time.time()

    print(f"Iniciando {NUM_THREADS} threads por {DURATION_SECS // 60} minutos...")
    print(f"Início: {datetime.now().strftime('%H:%M:%S')}\n")

    threads = []
    for _ in range(NUM_THREADS):
        t = threading.Thread(target=worker, daemon=True)
        t.start()
        threads.append(t)

    try:
        time.sleep(DURATION_SECS)
    except KeyboardInterrupt:
        print("\n[interrompido pelo usuário]")

    print("\nEncerrando threads...")
    stop_event.set()

    for t in threads:
        t.join(timeout=10)

    elapsed = time.time() - START_TIME
    print(f"\nFinalizado: {datetime.now().strftime('%H:%M:%S')}")
    print(f"Duração: {int(elapsed)}s")
    print(f"Total de queries: {counter['total']}")
    print(f"Erros: {counter['errors']}")
    print(f"Throughput médio: {counter['total'] / elapsed:.1f} queries/s")
    print("\nAcesse no Atlas UI:")
    print("  → Performance Advisor: sugestões de índices")
    print("  → Profiler: queries lentas com execution plan")
