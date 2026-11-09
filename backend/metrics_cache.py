"""Single-flight cache: fast UI polling must not multiply outbound Atlas reads."""
import time
from threading import Lock


class MetricsCache:
    def __init__(self):
        self._lock = Lock()
        self._entries = {}
        self._locks = {}

    def get(self, key, ttl, fetch):
        with self._lock:
            lock = self._locks.setdefault(key, Lock())
        with lock:
            now = time.monotonic()
            entry = self._entries.get(key)
            if entry and entry[0] > now:
                return entry[1]
            value = fetch()
            self._entries[key] = (time.monotonic() + ttl, value)
            return value


cache = MetricsCache()
