"""
api.py — Torre Atlas Control Plane · FastAPI backend
Exposes the Python logic (atlas_client / ai_agent / chat_memory) as a REST API
for the React + LeafyGreen frontend to consume via axios.

Credentials ALWAYS come from the environment (.env) — never from the frontend.
Run with:  uvicorn api:app --reload --port 8765
"""

import hashlib
import json
import hmac
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from typing import Literal, Optional
from uuid import uuid4

import requests
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from cluster_insights import collect as collect_insights
from metrics_cache import cache as metrics_cache
import observability
from atlas_client import (
    AtlasClient, create_index_direct,
    DEDICATED_TIERS, NVME_TIERS, TIER_PRICING_USD,
)
from ai_agent import generate_pdf_report
from llm_gateway import gateway_configured

load_dotenv()
observability.setup_logging()
logger = logging.getLogger("torre")

# ── Config ────────────────────────────────────────────────────────────────────
USD_BRL = float(os.getenv("USD_BRL", "5.70"))

REGION_NAMES = {
    "US_EAST_1": "AWS · N. Virginia", "US_EAST_2": "AWS · Ohio",
    "US_WEST_1": "AWS · N. California", "US_WEST_2": "AWS · Oregon",
    "SA_EAST_1": "AWS · São Paulo", "EU_WEST_1": "AWS · Ireland",
    "EU_WEST_2": "AWS · London", "EU_CENTRAL_1": "AWS · Frankfurt",
    "AP_SOUTHEAST_1": "AWS · Singapore", "AP_SOUTHEAST_2": "AWS · Sydney",
    "AP_SOUTH_1": "AWS · Mumbai", "AP_NORTHEAST_1": "AWS · Tokyo",
    "CA_CENTRAL_1": "AWS · Canadá",
}

def pretty_region(code: str) -> str:
    if not code or code == "—":
        return "—"
    return REGION_NAMES.get(code, code.replace("_", " ").title())


def _uri_cluster_hash() -> str:
    """Unique hash of the cluster MONGODB_URI points to ('' if unset/unparseable)."""
    m = re.search(r'\.([a-z0-9]+)\.mongodb\.net', os.getenv("MONGODB_URI", "").lower())
    return m.group(1) if m else ""


def _cluster_srv_hash(cluster: dict) -> str:
    srv = (cluster.get("connectionStrings") or {}).get("standardSrv", "") or cluster.get("srvAddress", "")
    m = re.search(r'\.([a-z0-9]+)\.mongodb\.net', str(srv).lower())
    return m.group(1) if m else ""


def _cpu_24h_stats(client: AtlasClient, project_id: str, process_id: str) -> Optional[dict]:
    """avg/p95 of normalized CPU over the last 24h — scaling and efficiency
    verdicts must not rely on a 5-min snapshot."""
    series = client.get_measurements_series(project_id, process_id)
    if "error" in series:
        return None
    vals = [v for v in series.get("cpu", []) if v is not None]
    if not vals:
        return None
    ordered = sorted(vals)
    return {"avg": round(sum(vals) / len(vals), 1),
            "p95": round(ordered[int(0.95 * (len(ordered) - 1))], 1)}


# ── Atlas client (singleton built from the environment) ───────────────────────
# One AtlasClient (and its requests.Session, with the Digest Auth handshake
# state) reused across every request in the process — rebuilding it per-request
# was defeating the session pooling the client promises. Rebuilt only if the
# env credentials actually change (simple hash check); otherwise a process
# restart is what picks up new credentials, which is fine for a local PoV.
_client_singleton: Optional[AtlasClient] = None
_client_singleton_key: Optional[str] = None
_atlas_client_lock = Lock()


def _client_credentials_key() -> str:
    raw = "|".join([
        os.getenv("ATLAS_PUBLIC_KEY", ""),
        os.getenv("ATLAS_PRIVATE_KEY", ""),
        os.getenv("ATLAS_ORG_ID", ""),
        os.getenv("ATLAS_PROJECT_ID", ""),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def get_client() -> AtlasClient:
    global _client_singleton, _client_singleton_key
    with _atlas_client_lock:
        pub  = os.getenv("ATLAS_PUBLIC_KEY", "")
        priv = os.getenv("ATLAS_PRIVATE_KEY", "")
        org  = os.getenv("ATLAS_ORG_ID", "")
        proj = os.getenv("ATLAS_PROJECT_ID", "")
        if not (pub and priv and (org or proj)):
            raise HTTPException(status_code=503, detail="Credenciais Atlas ausentes no servidor (.env).")
        key = _client_credentials_key()
        if _client_singleton is None or _client_singleton_key != key:
            _client_singleton = AtlasClient(pub, priv, org, proj)
            _client_singleton_key = key
        return _client_singleton


# ── Input boundary for Atlas identifiers ─────────────────────────────────────
# Path params and body ids are interpolated into Admin API URLs: anything outside the
# Atlas alphabet (dot segments, "?", JSON operators, unicode, megabyte strings) is a 422
# here, before any outbound call.
PROJECT_ID_PATTERN = r"^[A-Za-z0-9]*$"          # Atlas project ids: 24 hex chars
CLUSTER_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9-]*$|^$"  # Atlas: ASCII letters, digits, hyphen
_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9]{1,64}$")
_CLUSTER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,63}$")


def _validate_atlas_path_params(request: Request):
    params = request.path_params
    if "project_id" in params and not _PROJECT_ID_RE.fullmatch(params["project_id"]):
        raise HTTPException(status_code=422, detail="project_id inválido: use o id do projeto Atlas.")
    if "cluster_name" in params and not _CLUSTER_NAME_RE.fullmatch(params["cluster_name"]):
        raise HTTPException(status_code=422, detail="cluster_name inválido: use letras, números e hífen (até 64).")


# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="Torre Atlas Control Plane API", version="3.2.1",
              dependencies=[Depends(_validate_atlas_path_params)])


@app.exception_handler(requests.RequestException)
async def _atlas_unavailable(request: Request, exc: requests.RequestException):
    """Atlas Admin API 5xx/timeout/connection errors: sanitized 502, never a bare 500."""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    logger.warning("Atlas Admin API falhou path=%s status=%s err=%s", request.url.path, status, type(exc).__name__)
    detail = (f"Atlas Admin API respondeu HTTP {status}." if status else "Atlas Admin API não respondeu a tempo.")
    return JSONResponse(status_code=502, content={"detail": detail + " Tente novamente em instantes; nenhum dado foi alterado."})

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv(
        "CORS_ORIGINS", "http://localhost:5290,http://127.0.0.1:5290"
    ).split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Conversation-Id"],
)


# ── Auth ──────────────────────────────────────────────────────────────────────
# The server holds real Atlas API keys and Mongo credentials; every /api/* route
# can trigger billable/destructive actions (scale, index, explain, delete chat
# history). CORS only stops browsers — a direct HTTP client bypasses it entirely.
# If API_AUTH_TOKEN is set, all /api requests must present it; unset preserves
# today's local-dev behavior.
_API_AUTH_TOKEN = os.getenv("API_AUTH_TOKEN", "")

@app.middleware("http")
async def _require_api_token(request, call_next):
    public_operational_paths = {"/api/health", "/health/live"}
    protected_path = request.url.path.startswith("/api") or request.url.path == "/metrics"
    if (
        _API_AUTH_TOKEN
        and protected_path
        and request.url.path not in public_operational_paths
    ):
        supplied = request.headers.get("authorization", "")
        if not hmac.compare_digest(supplied, f"Bearer {_API_AUTH_TOKEN}"):
            return Response(status_code=401, content="Unauthorized")
    return await call_next(request)


@app.middleware("http")
async def _request_observability(request: Request, call_next):
    """request_id on every response + per-route latency/error counters at /api/metrics."""
    request_id = request.headers.get("x-request-id") or uuid4().hex[:16]
    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        observability.metrics.observe(request.url.path, 500, (time.perf_counter() - start) * 1000)
        logger.exception("unhandled error request_id=%s path=%s", request_id, request.url.path)
        raise
    elapsed_ms = (time.perf_counter() - start) * 1000
    observability.metrics.observe(request.url.path, response.status_code, elapsed_ms)
    response.headers["X-Request-Id"] = request_id
    return response


@app.get("/api/metrics")
def api_metrics():
    """In-process counters: requests/errors/latency per route + business counters."""
    return observability.metrics.snapshot()


@app.get("/metrics", include_in_schema=False)
def prometheus_metrics():
    return Response(observability.metrics.prometheus(), media_type="text/plain; version=0.0.4")


@app.get("/api/health")
def api_health():
    components = {"atlas_admin_api": "ready", "mongodb": "ready"}
    try:
        get_client().get_projects()
    except Exception:
        components["atlas_admin_api"] = "unavailable"
        logger.warning("healthcheck Atlas Admin API unavailable", exc_info=True)
    try:
        uri = os.getenv("MONGODB_URI", "")
        if not uri:
            raise RuntimeError("MONGODB_URI ausente")
        _mongo(uri).admin.command("ping")
    except Exception:
        components["mongodb"] = "unavailable"
        logger.warning("healthcheck MongoDB unavailable", exc_info=True)
    ready = all(value == "ready" for value in components.values())
    return JSONResponse(
        {"status": "ready" if ready else "degraded", "components": components},
        status_code=200 if ready else 503,
    )


@app.get("/health/live")
def api_liveness():
    return {"status": "alive"}


# ── MongoDB (cached client — connection pool reused across requests) ──────────
_mongo_clients: dict = {}
_mongo_clients_lock = Lock()

def _mongo(uri: str):
    from pymongo import MongoClient
    with _mongo_clients_lock:
        if uri not in _mongo_clients:
            _mongo_clients[uri] = MongoClient(uri, serverSelectionTimeoutMS=6000)
    return _mongo_clients[uri]


# ── Health / config ───────────────────────────────────────────────────────────
@app.get("/api/config")
def config():
    """Which integrations are configured on the server (without exposing secrets)."""
    return {
        "atlas":     bool(os.getenv("ATLAS_PUBLIC_KEY") and os.getenv("ATLAS_PRIVATE_KEY")),
        # Kept as "anthropic" for UI compatibility: true only when the Grove gateway is set.
        "anthropic": gateway_configured(),
        "llm_gateway": gateway_configured(),
        "mongodb":   bool(os.getenv("MONGODB_URI")),
        "usd_brl":   USD_BRL,
        "tiers":     {"dedicated": DEDICATED_TIERS, "nvme": NVME_TIERS},
        "pricing":   TIER_PRICING_USD,
    }


# ── Clusters ──────────────────────────────────────────────────────────────────
@app.get("/api/clusters")
async def list_clusters():
    # _list_clusters_sync does synchronous network I/O (requests, not httpx) —
    # offload to the threadpool so it doesn't block the Uvicorn worker's
    # event loop for the whole duration of the Atlas API round-trips.
    return await run_in_threadpool(_list_clusters_sync)


def _list_clusters_sync():
    client = get_client()
    rows = []
    uri_hash = _uri_cluster_hash()
    try:
        projects = client.get_projects()
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    for proj in projects:
        try:
            clusters = client.get_clusters(proj["id"])
        except Exception:
            logger.exception("get_clusters failed project_id=%s", proj["id"])
            continue
        for c in clusters:
            tier = region = "—"
            autoscale = {}
            try:
                rc = c["replicationSpecs"][0]["regionConfigs"][0]
                tier = rc["electableSpecs"]["instanceSize"]
                region = rc["regionName"]
                autoscale = rc.get("autoScaling") or {}
            except (KeyError, IndexError, TypeError):
                tier = "Free/Shared"
            status = "PAUSED" if c.get("paused") else c.get("stateName", "—")
            cost = AtlasClient.estimate_cost(tier, USD_BRL)
            compute = autoscale.get("compute") or {}
            rows.append({
                "project_id": proj["id"], "project_name": proj["name"],
                "cluster_name": c["name"], "tier": tier,
                "region": region, "region_pretty": pretty_region(region),
                "status": status, "mongo_version": c.get("mongoDBVersion", "—"),
                "cluster_type": c.get("clusterType", "—"),
                "cost_usd": cost["usd"], "cost_brl": cost["brl"],
                # whether MONGODB_URI points to THIS cluster (gates index/explain actions)
                "is_uri_target": bool(uri_hash) and _cluster_srv_hash(c) == uri_hash,
                "autoscale_compute": bool(compute.get("enabled")),
                "autoscale_min": compute.get("minInstanceSize", ""),
                "autoscale_max": compute.get("maxInstanceSize", ""),
                "autoscale_disk": bool((autoscale.get("diskGB") or {}).get("enabled")),
            })
    return {"clusters": rows}


@app.get("/api/alerts")
def alerts(project_ids: str = Query("", description="IDs separados por vírgula")):
    client = get_client()
    total = 0
    for pid in [p for p in project_ids.split(",") if p]:
        try:
            total += len(client.get_open_alerts(pid))
        except Exception:
            logger.exception("get_open_alerts failed project_id=%s", pid)
    return {"open_alerts": total}


@app.get("/api/invoice")
def invoice():
    client = get_client()
    if not client.org_id:
        return {"amount_usd": None, "status": "unavailable", "reason": "ATLAS_ORG_ID ausente; estimativas não são fatura."}
    inv = client.get_pending_invoice()
    cents = inv.get("amountBilledCents") if inv else None
    return {"amount_usd": cents / 100 if cents is not None else None,
            "status": "available" if cents is not None else "unavailable"}


# ── Performance Advisor / Profiler / Metrics ──────────────────────────────────
def _primary_or_404(client, project_id, cluster_name):
    pid = client.get_primary(project_id, cluster_name)
    if not pid:
        raise HTTPException(status_code=404, detail="Processo primário não encontrado (cluster pausado?).")
    return pid


@app.get("/api/cluster/{project_id}/{cluster_name}/pa")
def perf_advisor(project_id: str, cluster_name: str):
    client = get_client()
    pid = _primary_or_404(client, project_id, cluster_name)
    return client.get_suggested_indexes(project_id, pid)


SLOW_LOG_DEFAULT_LINES = 1000
SLOW_LOG_MAX_LINES = 5000


@app.get("/api/cluster/{project_id}/{cluster_name}/slow")
def slow_queries(project_id: str, cluster_name: str,
                 limit: int = Query(SLOW_LOG_DEFAULT_LINES, ge=1, le=SLOW_LOG_MAX_LINES)):
    # The raw slow log can reach 20k lines (~12 MB) on the demo cluster; the Profiler only
    # groups shapes, so ask Atlas for the most recent `limit` lines and cap again here in case
    # the API ignores nLogs.
    client = get_client()
    pid = _primary_or_404(client, project_id, cluster_name)
    data = client.get_slow_queries(project_id, pid, n_logs=limit) or {}
    rows = data.get("slowQueries") or []
    return {**data, "slowQueries": rows[:limit], "limit": limit, "truncated": len(rows) > limit}


@app.get("/api/cluster/{project_id}/{cluster_name}/measurements")
def measurements(project_id: str, cluster_name: str):
    client = get_client()
    pid = _primary_or_404(client, project_id, cluster_name)
    return client.get_measurements(project_id, pid)


@app.get("/api/cluster/{project_id}/{cluster_name}/series")
def series(project_id: str, cluster_name: str):
    client = get_client()
    pid = _primary_or_404(client, project_id, cluster_name)
    return client.get_measurements_series(project_id, pid)


@app.get("/api/cluster/{project_id}/{cluster_name}/health")
def health(project_id: str, cluster_name: str, status: str = "", mongo_version: str = "0"):
    client = get_client()
    pid = client.get_primary(project_id, cluster_name)
    if not pid:
        # Paused/transitioning cluster: PA and profiler have no process to report
        # on — an honest "no grade" beats scoring 90 on absent data.
        return {"score": None, "grade": "—", "color": "#7fa8bc", "n_pa": 0, "n_sq": 0,
                "components": [],
                "issues": ["Cluster pausado ou em transição — sem dados de PA/profiler."],
                "tips": [{"gain": 0, "text": "Retome o cluster para calcular o Health Score — "
                          "pausado, não há processo primário para o Performance Advisor e o profiler avaliarem."}]}

    n_pa = n_sq = 0
    collscan_shapes = set()
    try:
        n_pa = len(client.get_suggested_indexes(project_id, pid).get("suggestedIndexes", []))
        slow = client.get_slow_queries(project_id, pid).get("slowQueries", [])
        n_sq = len(slow)
        for q in slow:
            try:
                attr = json.loads(q.get("line") or "{}").get("attr", {})
            except Exception:
                attr = {}
            if "COLLSCAN" in str(attr.get("planSummary", "")):
                collscan_shapes.add((q.get("namespace", ""), str(attr.get("type", ""))))
    except Exception:
        logger.exception("health score data gathering failed project_id=%s cluster=%s", project_id, cluster_name)

    try:
        major = int(str(mongo_version).split(".")[0])
    except Exception:
        major = 0

    idx_pen = min(n_pa * 5, 30)
    # Penalize by COLLSCAN shape, not raw log volume — a busy, healthy cluster
    # always has slow-log entries; what hurts is repeated unindexed shapes.
    q_pen = min(len(collscan_shapes) * 10, 30)
    if q_pen == 0 and n_sq > 50:
        q_pen = 5
    ver_pts = 10 if major >= 8 else 5 if major == 7 else 0

    components = [
        {"label": "Status do Cluster", "earned": 20 if status == "IDLE" else 10, "max": 20,
         "detail": "IDLE = estável" if status == "IDLE" else f"Status {status} (em transição)",
         "ok": status == "IDLE"},
        {"label": "Saúde de Índices", "earned": 30 - idx_pen, "max": 30,
         "detail": "Nenhum índice sugerido pelo PA" if n_pa == 0 else f"{n_pa} índice(s) sugerido(s) pelo PA",
         "ok": n_pa == 0},
        {"label": "Saúde de Queries", "earned": 30 - q_pen, "max": 30,
         "detail": (f"{len(collscan_shapes)} shape(s) COLLSCAN em {n_sq} slow queries" if collscan_shapes
                    else f"Volume alto de slow queries ({n_sq}), sem COLLSCAN" if q_pen
                    else f"Sem COLLSCANs ({n_sq} slow queries no log)"),
         "ok": q_pen == 0},
        {"label": "Versão do MongoDB", "earned": ver_pts, "max": 10,
         "detail": f"MongoDB {mongo_version}" + ("" if major >= 8 else
                    " — 8.0 é a versão atual" if major == 7 else " — desatualizada (<7)"),
         "ok": major >= 8},
        {"label": "Base", "earned": 10, "max": 10, "detail": "pontuação base", "ok": True},
    ]
    score = sum(c["earned"] for c in components)
    grade = "A" if score >= 90 else "B" if score >= 75 else "C" if score >= 60 else "D" if score >= 40 else "F"
    color = "#00ED64" if score >= 75 else "#FFA500" if score >= 50 else "#FF4444"
    issues = [f"{c['label']}: {c['detail']} (-{c['max'] - c['earned']} pts)"
              for c in components if not c["ok"]]

    tips = []
    if n_pa:
        tips.append({"gain": idx_pen, "text": f"Crie os {n_pa} índice(s) sugeridos no **Performance Advisor** — recupera até +{idx_pen} pts."})
    if collscan_shapes:
        tips.append({"gain": q_pen, "text": f"Elimine os {len(collscan_shapes)} shape(s) COLLSCAN (veja o **Query Profiler**) — recupera até +{q_pen} pts."})
    elif q_pen:
        tips.append({"gain": q_pen, "text": f"Volume alto de slow queries ({n_sq}) — revise no **Query Profiler** para recuperar +{q_pen} pts."})
    if major < 8:
        tips.append({"gain": 10 - ver_pts, "text": f"Atualize o MongoDB {mongo_version} → 8.0 — recupera +{10 - ver_pts} pts e melhora performance."})
    if status != "IDLE":
        tips.append({"gain": 10, "text": f"Cluster em {status} — a nota volta a +10 pts quando estabilizar em IDLE."})
    if not tips:
        tips.append({"gain": 0, "text": "Cluster já está no topo — nenhuma ação necessária. 🎉"})

    return {"score": score, "grade": grade, "color": color, "n_pa": n_pa, "n_sq": n_sq,
            "components": components, "issues": issues, "tips": tips}


@app.get("/api/finops")
async def finops():
    # Same reasoning as list_clusters: run the whole synchronous, thread-pooled
    # evaluation off the event loop so the worker isn't blocked waiting on it.
    return await run_in_threadpool(_finops_sync)


def _finops_sync():
    """Evaluates cost efficiency (estimated cost vs. 24h average CPU) per cluster."""
    client = get_client()
    try:
        projects = client.get_projects()
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

    work = []
    for proj in projects:
        try:
            work += [(proj, c) for c in client.get_clusters(proj["id"])]
        except Exception:
            logger.exception("get_clusters failed project_id=%s", proj["id"])
            continue

    def _tier_down(tier: str) -> Optional[str]:
        tiers = NVME_TIERS if tier.endswith("_NVME") else DEDICATED_TIERS
        i = tiers.index(tier) if tier in tiers else -1
        return tiers[i - 1] if i > 0 else None

    def evaluate(item):
        proj, c = item
        try:
            rc = c["replicationSpecs"][0]["regionConfigs"][0]
            tier = rc["electableSpecs"]["instanceSize"]
        except (KeyError, IndexError, TypeError):
            tier = "Free/Shared"
        insight = collect_insights(client, proj["id"], c["name"], tier)
        cost = AtlasClient.estimate_cost(tier, USD_BRL)
        down = _tier_down(tier)
        savings = max(0, cost["usd"] - AtlasClient.estimate_cost(down, USD_BRL)["usd"]) if insight["action"] == "down" and down else 0
        return {**insight, "project_id": proj["id"], "project": proj["name"],
                "cluster": c["name"], "tier": tier, "cost_usd": cost["usd"], "cost_brl": cost["brl"],
                "savings_usd": savings, "target_tier": down if savings else None}

    # Each cluster needs 3-4 Atlas API calls — sequential would take minutes on a real org
    with ThreadPoolExecutor(max_workers=8) as ex:
        out = list(ex.map(evaluate, work))

    return {"clusters": out, "total_usd": sum(c["cost_usd"] for c in out),
            "potential_savings_usd": sum(c["savings_usd"] for c in out)}


@app.get("/api/cluster/{project_id}/{cluster_name}/scaling")
def scaling(project_id: str, cluster_name: str, tier: Optional[str] = Query(None, max_length=32)):
    client = get_client()
    if not tier:  # the UI always sends it; API callers may omit it and get the cluster's own tier
        try:
            tier = client.get_cluster(project_id, cluster_name)["replicationSpecs"][0]["regionConfigs"][0]["electableSpecs"]["instanceSize"]
        except Exception:  # noqa: BLE001 — collect_insights reports the Atlas failure itself
            tier = None
    insight = metrics_cache.get((_client_credentials_key(), "scaling", project_id, cluster_name, tier), 60,
        lambda: collect_insights(client, project_id, cluster_name, tier))
    # Keep the existing UI contract, but use worst-node CPU and avoid false OK.
    snapshot = next((n.get("snapshot", {}) for n in insight["nodes"] if n.get("role") == "REPLICA_PRIMARY"), {})
    metrics = {**snapshot, "cpu_p95_24h": insight["worst_node_p95"], "cpu_avg_24h": insight["cpu"],
               "conn_pct": round(snapshot.get("connections", 0) / AtlasClient.TIER_CONN_LIMIT.get(tier, 1500) * 100, 1)}
    return {**insight, "tier": tier, "headline": insight["verdict"],
            "severity": "high" if insight["action"] == "up" else "med" if insight["action"] in ("unknown", "investigate") else "low",
            "metrics": metrics if snapshot and "error" not in snapshot else None}



@app.get("/api/cluster/{project_id}/{cluster_name}/live")
def live_metrics(project_id: str, cluster_name: str, tier: str):
    client = get_client()
    def fetch():
        processes = client.get_cluster_processes(project_id, cluster_name)
        def node(p):
            pid = p.get("id") or f"{p['hostname']}:{p['port']}"
            return {"alias": p.get("userAlias") or p["hostname"], "role": p.get("typeName"),
                    "snapshot": client.get_measurements(project_id, pid)}
        with ThreadPoolExecutor(max_workers=3) as pool:
            nodes = list(pool.map(node, processes))
        from datetime import datetime, timezone
        return {"nodes": nodes, "fetched_at": datetime.now(timezone.utc).isoformat(),
                "source": "Atlas Admin API", "connection_limit": AtlasClient.TIER_CONN_LIMIT.get(tier), "granularity_seconds": 60, "refresh_seconds": 5}
    return metrics_cache.get((_client_credentials_key(), "live", project_id, cluster_name, tier), 1, fetch)


# ── Scale / Index (actions) ───────────────────────────────────────────────────
class ScaleBody(BaseModel):
    new_tier: str = Field(..., min_length=3, max_length=16)

@app.post("/api/cluster/{project_id}/{cluster_name}/scale")
def scale(project_id: str, cluster_name: str, body: ScaleBody):
    if body.new_tier not in DEDICATED_TIERS + NVME_TIERS:
        raise HTTPException(status_code=400, detail=f"Tier inválido: {body.new_tier}")
    client = get_client()
    try:
        result = client.scale_cluster(project_id, cluster_name, body.new_tier)
        return {"ok": True, "state": result.get("stateName", "UPDATING")}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


# project_id/cluster_name are REQUIRED: optional fields would let a caller
# bypass _assert_uri_targets simply by omitting them.
class IndexBody(BaseModel):
    namespace: str = Field(..., min_length=3, max_length=255)
    index_keys: list[dict[str, int | str]] = Field(..., min_length=1, max_length=10)
    project_id: str = Field(..., min_length=1, max_length=64, pattern=PROJECT_ID_PATTERN)
    cluster_name: str = Field(..., min_length=1, max_length=64, pattern=CLUSTER_NAME_PATTERN)

class ExplainBody(BaseModel):
    namespace: str = Field(..., min_length=3, max_length=255)
    filter: dict = Field(default_factory=dict)
    project_id: str = Field(..., min_length=1, max_length=64, pattern=PROJECT_ID_PATTERN)
    cluster_name: str = Field(..., min_length=1, max_length=64, pattern=CLUSTER_NAME_PATTERN)


def _assert_uri_targets(project_id: Optional[str], cluster_name: Optional[str]):
    """409 if MONGODB_URI points to a different cluster than the one selected in
    the UI — otherwise the index/explain would silently run on the wrong cluster."""
    uri_hash = _uri_cluster_hash()
    if not (uri_hash and project_id and cluster_name):
        raise HTTPException(
            status_code=503,
            detail="Não foi possível validar o cluster-alvo da operação.",
        )
    try:
        cluster = get_client().get_cluster(project_id, cluster_name)
    except Exception:
        logger.exception("get_cluster failed project_id=%s cluster=%s", project_id, cluster_name)
        raise HTTPException(
            status_code=503,
            detail="Atlas Admin API indisponível; operação recusada por segurança.",
        )
    srv_hash = _cluster_srv_hash(cluster)
    if not srv_hash:
        raise HTTPException(
            status_code=503,
            detail="Atlas não retornou o endereço do cluster; operação recusada por segurança.",
        )
    if srv_hash and srv_hash != uri_hash:
        raise HTTPException(status_code=409, detail=(
            f"MONGODB_URI aponta para outro cluster — a ação seria executada fora de "
            f"'{cluster_name}'. Ajuste o MONGODB_URI no .env do servidor."))


_NAMESPACE_RE = re.compile(r"^[A-Za-z0-9_-]{1,63}\.[A-Za-z0-9_-]{1,120}$")
_PROTECTED_DATABASES = {"admin", "config", "local"}


def _assert_namespace_is_safe(namespace: str):
    if not _NAMESPACE_RE.fullmatch(namespace):
        raise HTTPException(status_code=400, detail="Namespace deve usar o formato database.collection.")
    database = namespace.split(".", 1)[0].lower()
    if database in _PROTECTED_DATABASES:
        raise HTTPException(status_code=403, detail=f"Database protegido: {database}")


_INDEX_FIELD_RE = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_-]*)(?:\.[A-Za-z_][A-Za-z0-9_-]*)*$")
_INDEX_DIRECTIONS = {1, -1, "1", "-1", "hashed", "2dsphere", "text"}


def _assert_index_keys_are_safe(index_keys: list[dict[str, int | str]]):
    for key in index_keys:
        if len(key) != 1:
            raise HTTPException(status_code=400, detail="Cada chave de índice deve conter exatamente um campo.")
        field, direction = next(iter(key.items()))
        if not _INDEX_FIELD_RE.fullmatch(field):
            raise HTTPException(status_code=400, detail="Campo de índice inválido.")
        if direction not in _INDEX_DIRECTIONS:
            raise HTTPException(status_code=400, detail="Direção/tipo de índice inválido.")


_UNSAFE_FILTER_OPERATORS = {"$where", "$function", "$accumulator", "$expr"}

def _assert_filter_is_safe(value):
    """Rejects filters using $where/$function/$accumulator/$expr — these run
    attacker-supplied JavaScript/expressions server-side."""
    if isinstance(value, dict):
        for k, v in value.items():
            if k in _UNSAFE_FILTER_OPERATORS:
                raise HTTPException(status_code=400, detail=f"Operador não permitido: {k}")
            _assert_filter_is_safe(v)
    elif isinstance(value, list):
        for item in value:
            _assert_filter_is_safe(item)


@app.post("/api/explain")
def explain_query(body: ExplainBody):
    """Runs a real explain('executionStats') via pymongo on the given filter."""
    uri = os.getenv("MONGODB_URI", "")
    if not uri:
        raise HTTPException(status_code=400, detail="MONGODB_URI não configurado no servidor.")
    _assert_uri_targets(body.project_id, body.cluster_name)
    _assert_namespace_is_safe(body.namespace)
    _assert_filter_is_safe(body.filter)
    try:
        parts = body.namespace.split(".", 1)
        db_name, coll = parts[0], (parts[1] if len(parts) > 1 else parts[0])
        mc = _mongo(uri)
        plan = mc[db_name].command("explain", {"find": coll, "filter": body.filter, "maxTimeMS": 10000},
                                   verbosity="executionStats")
        exe = plan.get("executionStats", {})
        win = plan.get("queryPlanner", {}).get("winningPlan", {})
        return {
            "stage": win.get("stage") or win.get("inputStage", {}).get("stage", "—"),
            "docs_examined": exe.get("totalDocsExamined", "—"),
            "keys_examined": exe.get("totalKeysExamined", "—"),
            "n_returned": exe.get("nReturned", "—"),
            "exec_ms": exe.get("executionTimeMillis", "—"),
            "index_used": win.get("inputStage", {}).get("indexName") or win.get("indexName") or "COLLSCAN (sem índice)",
        }
    except Exception as e:
        # Driver errors can carry hostnames/URIs: log server-side, sanitize for the client.
        logger.warning("explain falhou namespace=%s err=%s", body.namespace, type(e).__name__)
        raise HTTPException(status_code=502, detail="Explain falhou no cluster (filtro inválido, prazo de 10 s ou permissão). Revise o filtro e tente novamente.")


@app.post("/api/index")
def create_index(body: IndexBody):
    uri = os.getenv("MONGODB_URI", "")
    if not uri:
        raise HTTPException(status_code=400, detail="MONGODB_URI não configurado no servidor.")
    _assert_uri_targets(body.project_id, body.cluster_name)
    _assert_namespace_is_safe(body.namespace)
    _assert_index_keys_are_safe(body.index_keys)
    return create_index_direct(uri, body.namespace, body.index_keys)


# ── Cost / estimate ───────────────────────────────────────────────────────────
@app.get("/api/cost")
def cost(tier: str):
    return AtlasClient.estimate_cost(tier, USD_BRL)


# ── AI: analysis (stream) and chat (stream) ───────────────────────────────────
class AnalyzeBody(BaseModel):
    project_id: str = Field(..., min_length=1, max_length=64, pattern=PROJECT_ID_PATTERN)
    cluster_name: str = Field(..., min_length=1, max_length=64, pattern=CLUSTER_NAME_PATTERN)

async def _mcp_text_response(messages, project_id, cluster_name, *, mode="chat", conversation_id=None):
    """Compatibility text streams use the same MCP runtime as the assistant."""
    from assistant_api import AssistantBody, assistant
    body = AssistantBody(session_id=uuid4().hex, project_id=project_id or "",
                         cluster_name=cluster_name or "", messages=messages,
                         conversation_id=conversation_id, mode=mode)
    response = await assistant(body)
    async def text():
        async for frame in response.body_iterator:
            if isinstance(frame, bytes):
                frame = frame.decode()
            for line in frame.splitlines():
                event = json.loads(line)
                if event["type"] == "text":
                    yield event["text"]
                elif event["type"] == "error":
                    yield "\n\n" + event["message"]
    return StreamingResponse(text(), media_type="text/plain", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@app.post("/api/analyze")
async def analyze(body: AnalyzeBody):
    return await _mcp_text_response([{"role": "user", "content": "Gere relatório técnico de performance e capacidade com evidências MCP, distinguindo fatos, hipóteses e dados ausentes."}],
                                    body.project_id, body.cluster_name, mode="report")


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(..., min_length=1, max_length=12_000)


class ChatBody(BaseModel):
    messages: list[ChatMessage] = Field(..., min_length=1, max_length=16)
    project_id: Optional[str] = Field(default=None, max_length=64, pattern=PROJECT_ID_PATTERN)
    cluster_name: Optional[str] = Field(default=None, max_length=64, pattern=CLUSTER_NAME_PATTERN)
    conversation_id: Optional[str] = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9_-]*$")


_OUT_OF_SCOPE_PATTERNS = (
    "temperatura", "previsao do tempo", "previsão do tempo", "clima hoje",
    "placar", "resultado do jogo", "receita culinaria", "receita culinária",
    "horoscopo", "horóscopo",
)


def _is_obviously_out_of_scope(text: str) -> bool:
    normalized = " ".join(text.lower().split())
    return any(pattern in normalized for pattern in _OUT_OF_SCOPE_PATTERNS)


def _scope_redirect() -> str:
    return (
        "Essa solicitação está fora do papel da Torre. Posso ajudar com clusters MongoDB Atlas, "
        "métricas de CPU/memória/IOPS, Performance Advisor, Query Profiler, custo, health score "
        "e recomendações de sizing baseadas nos dados disponíveis."
    )


def _with_ai_recovery(message: str) -> str:
    return (
        message
        + "\n\nEnquanto o modelo se recupera, os painéis de Overview, Health Score, "
        "Performance Advisor, Query Profiler, FinOps e Scale continuam disponíveis "
        "com os dados determinísticos da Atlas Admin API."
    )

_chat_db_ready = False

def _ensure_chat_db(uri: str):
    """Ensures the history collection's indexes exist, once per process."""
    global _chat_db_ready
    if not _chat_db_ready:
        from chat_memory import init_db
        init_db(uri)
        _chat_db_ready = True


# Atlas snapshot cache for /api/chat: 4 Atlas calls per chat turn is pure
# latency, and a stable snapshot lets the dynamic system block hit Anthropic's
# prompt cache across turns. Keyed per cluster, short TTL keeps data fresh.
_CHAT_SNAPSHOT_TTL = 150.0   # seconds (~2.5 min)
_chat_snapshots: dict = {}   # (project_id, cluster_name) -> (expires, (full_c, pa, sq, meas))


def _chat_cluster_snapshot(client: AtlasClient, project_id: str, cluster_name: str) -> tuple:
    key = (project_id, cluster_name)
    hit = _chat_snapshots.get(key)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    pid = client.get_primary(project_id, cluster_name)
    full_c = client.get_cluster(project_id, cluster_name)
    pa = client.get_suggested_indexes(project_id, pid) if pid else {}
    sq = client.get_slow_queries(project_id, pid) if pid else {}
    meas = client.get_measurements(project_id, pid) if pid else {}
    snapshot = (full_c, pa, sq, meas)
    _chat_snapshots[key] = (time.monotonic() + _CHAT_SNAPSHOT_TTL, snapshot)
    return snapshot


@app.post("/api/chat")
async def chat(body: ChatBody):
    messages = [message.model_dump() for message in body.messages]
    if _is_obviously_out_of_scope(messages[-1]["content"]):
        return StreamingResponse(iter([_scope_redirect()]), media_type="text/plain")
    return await _mcp_text_response(messages, body.project_id, body.cluster_name,
                                    conversation_id=body.conversation_id)


# ── Analysis PDF report (MongoDB branding, Markdown fallback) ─────────────────
class ReportBody(BaseModel):
    cluster_name: str = Field(..., min_length=1, max_length=64, pattern=CLUSTER_NAME_PATTERN)
    analysis: str = Field(..., min_length=1, max_length=100_000)
    health_score: Optional[int] = Field(default=None, ge=0, le=100)
    health_issues: Optional[list[str]] = Field(default=None, max_length=50)

@app.post("/api/report")
def report(body: ReportBody):
    data, mime, ext = generate_pdf_report(
        body.cluster_name, body.analysis, body.health_score, body.health_issues
    )
    return Response(
        content=data, media_type=mime,
        headers={"Content-Disposition": f'attachment; filename="torre-{body.cluster_name}.{ext}"'},
    )


# ── Chat history (Atlas persistence, optional) ────────────────────────────────
def _mongo_or_400():
    uri = os.getenv("MONGODB_URI", "")
    if not uri:
        raise HTTPException(status_code=400, detail="MONGODB_URI não configurado.")
    return uri

_MONGO_DOWN = "Cluster do MONGODB_URI inacessível (pausado ou IP fora da access list)."

@app.get("/api/chat/conversations")
def conversations(q: str = ""):
    uri = _mongo_or_400()
    from chat_memory import list_conversations, search_conversations
    try:
        rows = search_conversations(uri, q) if q else list_conversations(uri, limit=25)
    except Exception:
        raise HTTPException(status_code=503, detail=_MONGO_DOWN)
    return {"conversations": [
        {"id": r["id"], "title": r["title"], "cluster": r.get("cluster", ""),
         "msg_count": r.get("msg_count", 0), "updated_at": str(r.get("updated_at", ""))}
        for r in rows
    ]}

@app.get("/api/chat/conversations/{conv_id}")
def conversation_messages(conv_id: str):
    uri = _mongo_or_400()
    from chat_memory import load_messages
    try:
        msgs = load_messages(uri, conv_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        logger.exception("load_messages failed conv_id=%s", conv_id)
        raise HTTPException(status_code=503, detail=_MONGO_DOWN)
    return {"messages": [
        {"role": m.get("role"), "content": m.get("content"), "ts": str(m.get("ts", ""))}
        for m in msgs
    ]}

@app.delete("/api/chat/conversations/{conv_id}")
def delete_conversation_ep(conv_id: str):
    uri = _mongo_or_400()
    from chat_memory import delete_conversation
    try:
        delete_conversation(uri, conv_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        logger.exception("delete_conversation failed conv_id=%s", conv_id)
        raise HTTPException(status_code=503, detail=_MONGO_DOWN)
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    # Local-only by design — holds real Atlas/Mongo credentials. Override via
    # API_HOST if you know what you're doing (see API_AUTH_TOKEN in .env.example).
    uvicorn.run("api:app", host=os.getenv("API_HOST", "127.0.0.1"), port=8000, reload=True)


# Operational MCP assistant (same authentication and observability middleware).
from assistant_api import router as assistant_router
app.include_router(assistant_router)
