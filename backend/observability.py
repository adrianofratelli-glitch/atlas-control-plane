"""Lightweight observability: structured logs, request-id, in-process metrics.

No external dependency: an operator can answer "is the system degraded?" with
(a) JSON-aggregable logs, (b) a request_id correlating log <-> response, (c)
GET /api/metrics with per-route counters and latency. Production plugs
OpenTelemetry on top — the instrumentation points (request middleware +
per-endpoint counters) stay the same.
"""

import json
import logging
import os
import time
from collections import defaultdict

LOG_JSON = os.getenv("LOG_JSON", "0") == "1"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)[-1500:]
        rid = getattr(record, "request_id", None)
        if rid:
            entry["request_id"] = rid
        return json.dumps(entry, ensure_ascii=False)


def setup_logging() -> None:
    """LOG_JSON=1 -> stdout as JSON (aggregable); default: human-readable."""
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    if LOG_JSON:
        for handler in logging.getLogger().handlers:
            handler.setFormatter(JsonFormatter())


class Metrics:
    """Per-route counters/latency, in-process (exposed at /api/metrics).

    Enough for the PoV and a simple scrape; production swaps this for
    OTel/Prometheus while keeping the same instrumentation call sites.
    """

    def __init__(self) -> None:
        self.started_at = time.time()
        self.requests: dict = defaultdict(int)
        self.errors: dict = defaultdict(int)
        self.latency_ms_sum: dict = defaultdict(float)
        self.latency_ms_max: dict = defaultdict(float)
        self.counters: dict = defaultdict(int)

    def observe(self, route: str, status: int, elapsed_ms: float) -> None:
        self.requests[route] += 1
        if status >= 500:
            self.errors[route] += 1
        self.latency_ms_sum[route] += elapsed_ms
        self.latency_ms_max[route] = max(self.latency_ms_max[route], elapsed_ms)

    def bump(self, name: str, value: int = 1) -> None:
        """Business counters: chat_fallback, atlas_call_failed, mongo_down..."""
        self.counters[name] += value

    def snapshot(self) -> dict:
        routes = {}
        for route, count in sorted(self.requests.items()):
            routes[route] = {
                "requests": count,
                "errors_5xx": self.errors.get(route, 0),
                "avg_latency_ms": round(self.latency_ms_sum[route] / count, 1),
                "max_latency_ms": round(self.latency_ms_max[route], 1),
            }
        return {
            "uptime_seconds": round(time.time() - self.started_at, 1),
            "routes": routes,
            "counters": dict(sorted(self.counters.items())),
        }

    def prometheus(self) -> str:
        lines = ["# TYPE pov_uptime_seconds gauge", f"pov_uptime_seconds {time.time() - self.started_at:.3f}"]
        for route, count in sorted(self.requests.items()):
            label = route.replace("\\", "\\\\").replace('"', '\\"')
            lines.extend([f'pov_http_requests_total{{route="{label}"}} {count}', f'pov_http_errors_5xx_total{{route="{label}"}} {self.errors.get(route, 0)}', f'pov_http_latency_ms_sum{{route="{label}"}} {self.latency_ms_sum[route]:.3f}', f'pov_http_latency_ms_max{{route="{label}"}} {self.latency_ms_max[route]:.3f}'])
        for name, value in sorted(self.counters.items()):
            label = name.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'pov_business_counter{{name="{label}"}} {value}')
        return "\n".join(lines) + "\n"


metrics = Metrics()
