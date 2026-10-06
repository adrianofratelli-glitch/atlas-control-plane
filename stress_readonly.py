"""Bounded read-only stress of existing PoV data; no inserts or index changes.
Usage: python stress_readonly.py --minutes 6 --workers 6 --output /tmp/stress.json
"""
import argparse
import fcntl
import json
import os
import random
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv
from pymongo import MongoClient, ReadPreference
from pymongo.errors import ExecutionTimeout, PyMongoError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--minutes', type=float, default=6)
    parser.add_argument('--workers', type=int, default=6)
    parser.add_argument('--database', default='banco_inter')
    parser.add_argument('--output', default='/tmp/torre-stress.json')
    args = parser.parse_args()
    if not 0 < args.minutes <= 30 or not 1 <= args.workers <= 12:
        parser.error('minutes must be (0,30], workers [1,12]')
    # One workload per workspace, even if launchers overlap or a manual run exists.
    lock_path = Path(__file__).with_name('.assistant-state') / 'stress.lock'
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    workload_lock = lock_path.open('w')
    try:
        fcntl.flock(workload_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print('Stress já está ativo neste workspace; nova carga não iniciada.', flush=True)
        return
    load_dotenv(Path(__file__).with_name('.env'))
    uri = os.environ['MONGODB_URI']
    client = MongoClient(uri, appname='torre-readonly-stress', maxPoolSize=args.workers + 2,
                         serverSelectionTimeoutMS=10000, socketTimeoutMS=8000, connectTimeoutMS=5000)
    client.admin.command('ping')
    db = client[args.database]
    names = set(db.list_collection_names())
    if not {'transacoes', 'fatura'} <= names:
        raise SystemExit('Expected PoV collections are absent; no workload executed.')
    # Sample values in memory only, never output customer document data.
    samples = {name: list(db[name].find({}, {'account_number': 1, 'segmento': 1}).limit(20))
               for name in ('transacoes', 'fatura')}
    started = datetime.now(timezone.utc).isoformat()
    deadline = time.monotonic() + args.minutes * 60
    started_clock = time.monotonic()
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    lock = threading.Lock()
    results = {'completed': 0, 'timeouts': 0, 'errors': 0, 'primary_ops': 0, 'secondary_ops': 0}
    latency = []

    def worker(index):
        preference = ReadPreference.PRIMARY if index % 2 == 0 else ReadPreference.SECONDARY
        while not stop.is_set() and time.monotonic() < deadline:
            # Ramp 2 -> 4 -> configured max over thirds of the run.
            phase = min(2, int((time.monotonic() - started_clock) / (args.minutes * 20)))
            if index >= min(args.workers, (phase + 1) * 2):
                stop.wait(.2)
                continue
            name = random.choice(('transacoes', 'fatura'))
            collection = db[name].with_options(read_preference=preference)
            prefix = 'amos' if name == 'transacoes' else 'amss'
            sample = random.choice(samples[name]) if samples[name] else {}
            shape = random.randrange(4)
            started_op = time.monotonic()
            status = 'completed'
            try:
                if shape == 0:
                    list(collection.find({f'{prefix}_mt_type': random.choice(['d', 'c'])}, {'_id': 1}).limit(200).max_time_ms(5000).comment('torre-stress-type'))
                elif shape == 1:
                    list(collection.find({'account_number': sample.get('account_number', '__torre_missing__')}, {'_id': 1, f'{prefix}_mt_eff_date': 1}).sort(f'{prefix}_mt_eff_date', -1).limit(50).max_time_ms(5000).comment('torre-stress-sort'))
                elif shape == 2:
                    collection.count_documents({'segmento': sample.get('segmento', '__torre_missing__'), f'{prefix}_mt_type': 'd'}, maxTimeMS=5000, comment='torre-stress-count')
                else:
                # EXCEÇÃO EXPLÍCITA à regra "sem $regex": gerador de carga proposital. O regex case-insensitive
                # não usa os limites do índice e produz a query lenta que o Query Profiler/Performance Advisor
                # precisam mostrar na demo. Nunca copie este padrão para o caminho da app (texto = $search).
                    list(collection.find({f'{prefix}_mt_desc': {'$regex': '^AMAZON', '$options': 'i'}}, {'_id': 1}).limit(30).max_time_ms(5000).comment('torre-stress-regex'))
            except ExecutionTimeout:
                status = 'timeouts'
            except PyMongoError:
                status = 'errors'
            with lock:
                results[status] += 1
                results['primary_ops' if index % 2 == 0 else 'secondary_ops'] += 1
                latency.append((time.monotonic() - started_op) * 1000)
                # Fail closed on transport/auth errors, not intentional scan deadlines.
                if results['errors'] >= 10:
                    stop.set()
            stop.wait(.1)

    print(json.dumps({'started_at': started, 'minutes': args.minutes, 'workers': args.workers,
                      'read_only': True, 'ramp': '2 -> 4 -> max', 'max_time_ms': 5000}), flush=True)
    try:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = [pool.submit(worker, i) for i in range(args.workers)]
            for f in futures:
                f.result()
    except KeyboardInterrupt:
        stop.set()
    finally:
        client.close()
    ordered = sorted(latency)
    report = {**results, 'started_at': started, 'ended_at': datetime.now(timezone.utc).isoformat(),
              'duration_seconds': round(time.monotonic() - started_clock, 1), 'read_only': True,
              'max_workers': args.workers, 'stopped_on_errors': stop.is_set(),
              'client_latency_p95_ms': round(ordered[int(.95 * (len(ordered)-1))], 1) if ordered else None,
              'note': 'Client latency includes intentional maxTimeMS timeouts; synthetic load, not production sizing.'}
    Path(args.output).write_text(json.dumps(report, indent=2))
    print(json.dumps(report), flush=True)


if __name__ == '__main__':
    main()
