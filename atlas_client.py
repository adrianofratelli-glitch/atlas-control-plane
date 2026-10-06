import logging
import random
import time
import requests
from functools import wraps
from requests.auth import HTTPDigestAuth
from typing import Optional

logger = logging.getLogger("torre.atlas_client")

# ── Rate-limit resilience (Atlas Admin API: ~100 req/min per API key/org) ──
# Centralized retry wrapper for every outbound HTTP call: on 429, honor
# Retry-After if present, otherwise short exponential backoff + jitter.
# Propagates the exception only after exhausting attempts.
_RATE_LIMIT_MAX_ATTEMPTS = 3
_RATE_LIMIT_BASE_DELAY_S = 1.0


_TRANSIENT_STATUS = {502, 503, 504}


def _with_rate_limit_retry(fn):
    # GETs are idempotent: they also retry transient 502/503/504 and connection resets.
    # PATCH (tier change) only retries 429, which Atlas guarantees was not applied.
    idempotent = fn.__name__ == "_get"

    @wraps(fn)
    def wrapper(*args, **kwargs):
        attempt = 0
        while True:
            try:
                return fn(*args, **kwargs)
            except requests.ConnectionError:
                if not idempotent or attempt >= _RATE_LIMIT_MAX_ATTEMPTS - 1:
                    raise
                delay = _RATE_LIMIT_BASE_DELAY_S * (2 ** attempt) + random.uniform(0, 0.25)
                logger.warning("Atlas Admin API sem conexão — tentativa %d/%d, aguardando %.2fs",
                               attempt + 1, _RATE_LIMIT_MAX_ATTEMPTS, delay)
                time.sleep(delay)
                attempt += 1
                continue
            except requests.HTTPError as e:
                resp = e.response
                status = resp.status_code if resp is not None else None
                retryable = status == 429 or (idempotent and status in _TRANSIENT_STATUS)
                if not retryable or attempt >= _RATE_LIMIT_MAX_ATTEMPTS - 1:
                    raise
                retry_after = resp.headers.get("Retry-After")
                if retry_after:
                    try:
                        delay = float(retry_after)
                    except ValueError:
                        delay = _RATE_LIMIT_BASE_DELAY_S * (2 ** attempt)
                else:
                    delay = _RATE_LIMIT_BASE_DELAY_S * (2 ** attempt)
                delay += random.uniform(0, 0.25)
                logger.warning(
                    "Atlas Admin API %s — tentativa %d/%d, aguardando %.2fs",
                    status, attempt + 1, _RATE_LIMIT_MAX_ATTEMPTS, delay,
                )
                time.sleep(delay)
                attempt += 1
    return wrapper

ATLAS_BASE = "https://cloud.mongodb.com/api/atlas/v2"
ATLAS_HEADERS = {
    "Accept": "application/vnd.atlas.2024-08-05+json",
    "Content-Type": "application/vnd.atlas.2024-08-05+json",
}

DEDICATED_TIERS = ["M10","M20","M30","M40","M50","M60","M80","M140","M200","M300","M400","M700"]
NVME_TIERS      = ["M40_NVME","M50_NVME","M60_NVME","M80_NVME","M200_NVME","M400_NVME"]

# ── Monthly cost estimate (USD) ─────────────────────────────────────────────
# 3-node replica set · AWS us-east-1 · approximate values (atlas.mongodb.com/pricing)
# Vary by cloud provider and region — use as a comparative reference
TIER_PRICING_USD = {
    "M10":         57,
    "M20":        144,
    "M30":        389,
    "M40":        749,
    "M50":      1_440,
    "M60":      2_844,
    "M80":      5_256,
    "M140":     7_913,
    "M200":    10_505,
    "M300":    15_732,
    "M400":    21_024,
    "M700":    31_500,
    "M40_NVME":    843,
    "M50_NVME":  1_642,
    "M60_NVME":  3_276,
    "M80_NVME":  5_926,
    "M200_NVME": 11_836,
    "M400_NVME": 23_672,
    "Free/Shared":   0,
}


# Shared TTL caches (module-level: AtlasClient is rebuilt per request, so
# per-instance dicts would never produce a hit)
_PRIMARY_CACHE:  dict = {}   # (project_id, cluster_name) -> (expires, value)
_PROJECTS_CACHE: dict = {}   # cache key -> (expires, value)
_CLUSTERS_CACHE: dict = {}   # project_id -> (expires, value)


class AtlasClient:
    def __init__(self, public_key: str, private_key: str, org_id: str, project_id: str = ""):
        self.auth       = HTTPDigestAuth(public_key, private_key)
        self.org_id     = org_id
        self.project_id = project_id
        # Reused HTTP session: keeps the TCP/TLS connection (and digest-auth
        # handshake state) alive across calls — halves Atlas API latency.
        self.session    = requests.Session()
        # TTL caches (simple dict + time.monotonic — avoids re-hitting Atlas
        # for data that barely changes between dashboard requests)
        self._primary_cache  = _PRIMARY_CACHE
        self._projects_cache = _PROJECTS_CACHE
        self._clusters_cache = _CLUSTERS_CACHE

    _PRIMARY_TTL  = 60.0
    _LISTING_TTL  = 30.0

    @staticmethod
    def _cache_get(cache: dict, key):
        hit = cache.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        return None

    @staticmethod
    def _cache_put(cache: dict, key, value, ttl: float):
        cache[key] = (time.monotonic() + ttl, value)

    # ── HTTP helpers ──────────────────────────────────────────────────────
    @_with_rate_limit_retry
    def _get(self, path, params=None):
        r = self.session.get(
            f"{ATLAS_BASE}{path}", auth=self.auth,
            headers=ATLAS_HEADERS, params=params or {}, timeout=30
        )
        r.raise_for_status()
        return r.json()

    @_with_rate_limit_retry
    def _patch(self, path, body):
        r = self.session.patch(
            f"{ATLAS_BASE}{path}", auth=self.auth,
            headers=ATLAS_HEADERS, json=body, timeout=30
        )
        r.raise_for_status()
        return r.json()

    # ── Org / Projects ────────────────────────────────────────────────────
    def get_org(self):
        return self._get(f"/orgs/{self.org_id}")

    def get_projects(self):
        key = self.project_id or "_org"
        cached = self._cache_get(self._projects_cache, key)
        if cached is not None:
            return cached
        if self.project_id:
            result = [self._get(f"/groups/{self.project_id}")]
        else:
            result = self._get_all_pages("/groups", params={"itemsPerPage": 500})
        self._cache_put(self._projects_cache, key, result, self._LISTING_TTL)
        return result

    def _get_all_pages(self, path, params=None):
        """Follows the Admin API's `links` envelope (rel=next) until it stops
        offering a next page — otherwise orgs with more results than fit on
        one page silently get truncated (e.g. >500 projects)."""
        params = dict(params or {})
        params.setdefault("pageNum", 1)
        results = []
        page = params["pageNum"]
        while True:
            params["pageNum"] = page
            data = self._get(path, params=params)
            page_results = data.get("results", [])
            if not page_results:
                break
            results.extend(page_results)
            links = data.get("links", []) or []
            has_next = any(l.get("rel") == "next" for l in links)
            if not has_next:
                break
            page += 1
        return results

    # ── Clusters ──────────────────────────────────────────────────────────
    def get_clusters(self, project_id):
        cached = self._cache_get(self._clusters_cache, project_id)
        if cached is not None:
            return cached
        result = self._get(f"/groups/{project_id}/clusters").get("results", [])
        self._cache_put(self._clusters_cache, project_id, result, self._LISTING_TTL)
        return result

    def get_cluster(self, project_id, cluster_name):
        return self._get(f"/groups/{project_id}/clusters/{cluster_name}")

    def scale_cluster(self, project_id, cluster_name, new_tier):
        cluster   = self.get_cluster(project_id, cluster_name)
        rep_specs = cluster.get("replicationSpecs", [])
        for spec in rep_specs:
            for rc in spec.get("regionConfigs", []):
                for key in ("electableSpecs", "readOnlySpecs", "analyticsSpecs"):
                    if rc.get(key):
                        rc[key]["instanceSize"] = new_tier
        return self._patch(
            f"/groups/{project_id}/clusters/{cluster_name}",
            {"replicationSpecs": rep_specs}
        )

    # ── Processes / Primary ───────────────────────────────────────────────
    def get_processes(self, project_id):
        return self._get(f"/groups/{project_id}/processes").get("results", [])

    def get_primary(self, project_id: str, cluster_name: str) -> Optional[str]:
        """
        Find the primary process using the cluster's real connectionString.
        Needed because name matching fails for clusters with generic names
        like 'MongoDB' (which appears in every .mongodb.net hostname).

        Resolve processes through exact cluster aliases; shared suffixes are not identity.

        Cached for 60s per (project_id, cluster_name) — primaries change on
        election only, and every dashboard endpoint re-resolves it otherwise.
        """
        cache_key = (project_id, cluster_name)
        hit = self._primary_cache.get(cache_key)
        if hit and hit[0] > time.monotonic():   # value may legitimately be None
            return hit[1]
        result = self._get_primary_uncached(project_id, cluster_name)
        self._cache_put(self._primary_cache, cache_key, result, self._PRIMARY_TTL)
        return result

    def get_cluster_processes(self, project_id: str, cluster_name: str) -> list:
        """Match Atlas aliases, never a shared DNS suffix or arbitrary primary."""
        import re
        from urllib.parse import urlsplit
        cluster = self.get_cluster(project_id, cluster_name)
        if cluster.get("paused") or cluster.get("stateName") in ("PAUSED", "DELETING", "CREATING"):
            return []
        srv = (cluster.get("connectionStrings") or {}).get("standardSrv") or cluster.get("srvAddress", "")
        host = urlsplit(srv).hostname or ""
        domain = host.partition(".")[2]
        if not domain:
            return []
        pattern = re.compile(r"^" + re.escape(cluster_name.lower()) +
                             r"-shard-\d+-\d+\." + re.escape(domain.lower()) + r"$")
        processes = self.get_processes(project_id)
        return [p for p in processes
                if p.get("typeName") in ("REPLICA_PRIMARY", "REPLICA_SECONDARY")
                and any(pattern.fullmatch(p.get(key, "").lower())
                        for key in ("userAlias", "hostname"))]

    def _get_primary_uncached(self, project_id: str, cluster_name: str) -> Optional[str]:
        processes = self.get_cluster_processes(project_id, cluster_name)
        primary = next((p for p in processes if p.get("typeName") == "REPLICA_PRIMARY"), None)
        return (primary.get("id") or f"{primary['hostname']}:{primary['port']}") if primary else None

    # ── Disk / Storage measurements (endpoint /disks/{partition}) ─────────
    def _disk_metrics(self, project_id: str, process_id: str) -> dict:
        """Fetch IOPS, latency and storage usage % for the first partition.
        Separate endpoint from the process one. Best-effort — returns {} on failure."""
        try:
            disks = self._get(f"/groups/{project_id}/processes/{process_id}/disks").get("results", [])
            if not disks:
                return {}
            part = disks[0]["partitionName"]
            data = self._get(
                f"/groups/{project_id}/processes/{process_id}/disks/{part}/measurements",
                params={"granularity": "PT1M", "period": "PT5M", "m": [
                    "DISK_PARTITION_IOPS_READ", "DISK_PARTITION_IOPS_WRITE",
                    "DISK_PARTITION_LATENCY_READ", "DISK_PARTITION_LATENCY_WRITE",
                    "DISK_PARTITION_SPACE_PERCENT_USED",
                ]},
            )
        except Exception:
            logger.exception("disk metrics fetch failed project_id=%s process_id=%s", project_id, process_id)
            return {}

        def _last(points):
            for p in reversed(points):
                if p.get("value") is not None:
                    return round(p["value"], 2)
            return 0

        out = {}
        for m in data.get("measurements", []):
            n = m.get("name", "")
            v = _last(m.get("dataPoints", []))
            if "IOPS_READ" in n:        out["iops_read"]  = v
            elif "IOPS_WRITE" in n:     out["iops_write"] = v
            elif "LATENCY_READ" in n:   out["lat_read"]   = v
            elif "LATENCY_WRITE" in n:  out["lat_write"]  = v
            elif "SPACE_PERCENT" in n:  out["space_pct"]  = v
        return out

    # ── Hardware Measurements ─────────────────────────────────────────────
    def get_measurements(self, project_id: str, process_id: str) -> dict:
        """
        Fetch the primary's hardware metrics (recent 5-minute window).
        Uses NORMALIZED CPU (0–100% per core, matching the Atlas Real Time panel)
        and takes the last non-null value of each metric (Atlas lags ~1-2 min).

        Metrics: CPU, Memory, Connections, Disk IOPS + Latency, Network,
        Opcounters (query/insert/update/delete/getmore/command) and Query Targeting.
        """
        # IMPORTANT: the PROCESS endpoint only accepts system/process metrics.
        # DISK_PARTITION_* metrics belong to the /disks/{partition} endpoint —
        # including them here returns 404 and breaks the whole call (zeroes everything).
        METRICS = [
            # Normalized CPU (preferred) + non-normalized fallback
            "SYSTEM_NORMALIZED_CPU_USER", "SYSTEM_NORMALIZED_CPU_KERNEL",
            "SYSTEM_CPU_USER", "SYSTEM_CPU_KERNEL",
            # Memory
            "SYSTEM_MEMORY_USED", "SYSTEM_MEMORY_AVAILABLE",
            # Connections
            "CONNECTIONS",
            # Operations
            "OPCOUNTER_INSERT", "OPCOUNTER_QUERY", "OPCOUNTER_UPDATE",
            "OPCOUNTER_DELETE", "OPCOUNTER_GETMORE", "OPCOUNTER_CMD",
            # Network
            "NETWORK_BYTES_IN", "NETWORK_BYTES_OUT",
            # Query efficiency (scanned/returned — lower is better)
            "QUERY_TARGETING_SCANNED_OBJECTS_PER_RETURNED",
        ]
        try:
            data = self._get(
                f"/groups/{project_id}/processes/{process_id}/measurements",
                params={
                    "granularity": "PT1M",
                    "period":      "PT5M",
                    "m":           METRICS,
                },
            )
        except Exception as e:
            logger.exception("measurements fetch failed project_id=%s process_id=%s", project_id, process_id)
            return {"error": str(e)}

        def _last_nonnull(points):
            for p in reversed(points):
                if p.get("value") is not None:
                    return p["value"]
            return None

        result = {}
        observed = {}
        for m in data.get("measurements", []):
            name = m.get("name", "")
            val  = _last_nonnull(m.get("dataPoints", []))
            if val is not None:
                result[name] = round(val, 2)
                observed[name] = next((p.get("timestamp") for p in reversed(m.get("dataPoints", [])) if p.get("value") is not None), None)

        # CPU: prefer normalized (0–100%), fall back to non-normalized if absent
        cpu_user = result.get("SYSTEM_NORMALIZED_CPU_USER")
        cpu_kern = result.get("SYSTEM_NORMALIZED_CPU_KERNEL")
        if cpu_user is None and cpu_kern is None:
            cpu_user = result.get("SYSTEM_CPU_USER", 0)
            cpu_kern = result.get("SYSTEM_CPU_KERNEL", 0)

        mem_used  = result.get("SYSTEM_MEMORY_USED", 0)
        mem_avail = result.get("SYSTEM_MEMORY_AVAILABLE", 0)

        formatted = {}
        # SYSTEM_MEMORY_* comes in KB → GB = value / 1024² (1_048_576)
        formatted["cpu_pct"]          = round((cpu_user or 0) + (cpu_kern or 0), 1)
        formatted["memory_used_gb"]   = round(mem_used  / 1_048_576, 2) if mem_used  else 0
        formatted["memory_avail_gb"]  = round(mem_avail / 1_048_576, 2) if mem_avail else 0
        formatted["mem_total_gb"]     = round((mem_used + mem_avail) / 1_048_576, 1) if (mem_used or mem_avail) else 0
        formatted["mem_pct"]          = round(mem_used / (mem_used + mem_avail) * 100, 1) if (mem_used + mem_avail) else 0
        formatted["connections"]      = int(result.get("CONNECTIONS", 0))
        # Disk/storage come from the /disks endpoint (best-effort, won't break on failure)
        disk = self._disk_metrics(project_id, process_id)
        formatted["disk_iops_read"]   = disk.get("iops_read", 0)
        formatted["disk_iops_write"]  = disk.get("iops_write", 0)
        formatted["disk_lat_read"]    = disk.get("lat_read", 0)
        formatted["disk_lat_write"]   = disk.get("lat_write", 0)
        formatted["disk_pct"]         = disk.get("space_pct", 0)
        formatted["ops_insert"]       = result.get("OPCOUNTER_INSERT", 0)
        formatted["ops_query"]        = result.get("OPCOUNTER_QUERY",  0)
        formatted["ops_update"]       = result.get("OPCOUNTER_UPDATE", 0)
        formatted["ops_delete"]       = result.get("OPCOUNTER_DELETE", 0)
        formatted["ops_getmore"]      = result.get("OPCOUNTER_GETMORE", 0)
        formatted["ops_command"]      = result.get("OPCOUNTER_CMD", 0)
        formatted["net_in_mb"]        = round(result.get("NETWORK_BYTES_IN",  0) / 1_048_576, 2)
        formatted["net_out_mb"]       = round(result.get("NETWORK_BYTES_OUT", 0) / 1_048_576, 2)
        formatted["query_targeting"]  = result.get("QUERY_TARGETING_SCANNED_OBJECTS_PER_RETURNED", 0)
        formatted["_disk_available"]  = all(k in disk for k in ("space_pct", "iops_read", "iops_write"))
        formatted["_memory_available"] = bool(mem_used + mem_avail)
        formatted["cpu_observed_at"] = observed.get("SYSTEM_NORMALIZED_CPU_USER")
        formatted["_raw"]             = result
        return formatted

    # ── Hardware Measurements (time series) ───────────────────────────────
    def get_measurements_series(self, project_id: str, process_id: str,
                                period: str = "P1D", granularity: str = "PT1H",
                                start: str = None, end: str = None) -> dict:
        """
        Fetch a time series of metrics for charts (default: last 24h, 1 point/h).
        Returns a plot-ready structure: timestamps + aligned series.
        """
        # Normalized CPU (0–100% regardless of core count) — must match the
        # snapshot in get_measurements, otherwise chart and gauge contradict
        # each other on multi-core tiers.
        METRICS = [
            "SYSTEM_NORMALIZED_CPU_USER", "SYSTEM_NORMALIZED_CPU_KERNEL",
            "OPCOUNTER_QUERY", "OPCOUNTER_INSERT", "OPCOUNTER_UPDATE",
            "CONNECTIONS",
        ]
        try:
            data = self._get(
                f"/groups/{project_id}/processes/{process_id}/measurements",
                params={"granularity": granularity, "m": METRICS,
                        **({"start": start, "end": end} if start and end else {"period": period})},
            )
        except Exception as e:
            logger.exception("measurements series fetch failed project_id=%s process_id=%s", project_id, process_id)
            return {"error": str(e)}

        # Align by timestamp and preserve gaps: no telemetry is not zero load.
        raw = {m.get("name"): {p["timestamp"]: p.get("value")
               for p in m.get("dataPoints", []) if p.get("timestamp")}
               for m in data.get("measurements", [])}
        timestamps = sorted({t for values in raw.values() for t in values})

        def combine(a, b):
            out = []
            for t in timestamps:
                va, vb = raw.get(a, {}).get(t), raw.get(b, {}).get(t)
                out.append(round(va + vb, 1) if va is not None and vb is not None else None)
            return out

        def clean(name):
            return [round(raw[name][t], 1) if raw.get(name, {}).get(t) is not None else None
                    for t in timestamps]

        return {"timestamps": timestamps,
                "cpu": combine("SYSTEM_NORMALIZED_CPU_USER", "SYSTEM_NORMALIZED_CPU_KERNEL"),
                "ops_query": clean("OPCOUNTER_QUERY"), "ops_insert": clean("OPCOUNTER_INSERT"),
                "ops_update": clean("OPCOUNTER_UPDATE"), "connections": clean("CONNECTIONS")}

    # ── Scaling recommendation (heuristic, based on real metrics) ─────────
    # Approximate connection limit per tier (Atlas docs)
    TIER_CONN_LIMIT = {
        "M10": 1500, "M20": 3000, "M30": 3000, "M40": 6000, "M50": 16000,
        "M60": 32000, "M80": 96000, "M140": 96000, "M200": 128000,
        "M300": 128000, "M400": 128000, "M700": 128000,
        "M40_NVME": 6000, "M50_NVME": 16000, "M60_NVME": 32000,
        "M80_NVME": 96000, "M200_NVME": 128000, "M400_NVME": 128000,
    }

    @staticmethod
    def recommend_scaling(measurements: dict, tier: str, cpu24: dict = None) -> dict:
        """
        Recommend scaling based on real hardware metrics.
        cpu24 = {"avg": x, "p95": y} over the last 24h, when available — scaling
        decisions must not rely on a 5-min snapshot (a 9am demo would recommend
        downsizing a cluster that peaks at 3am).
        Returns {"action": "up"|"down"|"ok", "severity": "high"|"med"|"low",
                 "reasons": [str], "headline": str}.
        """
        if not measurements or "error" in measurements:
            return {"action": "ok", "severity": "low", "reasons": [], "headline": ""}

        cpu      = measurements.get("cpu_pct", 0)
        cpu_up   = cpu24["p95"] if cpu24 else cpu
        cpu_down = cpu24["avg"] if cpu24 else cpu
        cpu_lbl  = "CPU (p95 24h)" if cpu24 else "CPU"
        conns    = measurements.get("connections", 0)
        iops     = measurements.get("disk_iops_read", 0) + measurements.get("disk_iops_write", 0)
        mem_pct  = measurements.get("mem_pct", 0)
        disk_pct = measurements.get("disk_pct", 0)
        conn_limit = AtlasClient.TIER_CONN_LIMIT.get(tier, 1500)
        conn_pct   = (conns / conn_limit * 100) if conn_limit else 0

        reasons, action, severity = [], "ok", "low"

        # ── CPU ──
        if cpu_up >= 80:
            action, severity = "up", "high"
            reasons.append(f"{cpu_lbl} em **{cpu_up}%** — acima de 80%, gargalo de processamento")
        elif cpu_up >= 65:
            action, severity = "up", "med"
            reasons.append(f"{cpu_lbl} em **{cpu_up}%** — aproximando do limite (65%+)")

        # ── Memory (WiredTiger cache pressure) ──
        if mem_pct >= 90:
            action, severity = "up", "high"
            reasons.append(f"Memória em **{mem_pct}%** — working set não cabe na RAM, pressão de cache WiredTiger")
        elif mem_pct >= 75:
            if action != "up":
                action, severity = "up", "med"
            reasons.append(f"Memória em **{mem_pct}%** — aproximando do limite, risco de page faults em disco")

        # ── Storage (disk usage) ──
        if disk_pct >= 85:
            action, severity = "up", "high"
            reasons.append(f"Storage em **{disk_pct}%** — risco de esgotar disco; expanda o tier")
        elif disk_pct >= 70:
            if action != "up":
                action, severity = "up", "med"
            reasons.append(f"Storage em **{disk_pct}%** — planeje expansão de disco")

        # ── Connections ──
        if conn_pct >= 80:
            action, severity = "up", "high"
            reasons.append(f"Conexões em **{conns}** ({conn_pct:.0f}% do limite do {tier})")
        elif conn_pct >= 60:
            if action != "up":
                action, severity = "up", "med"
            reasons.append(f"Conexões em **{conns}** ({conn_pct:.0f}% do limite do {tier})")

        # ── Disk I/O ──
        if iops >= 3000 and "_NVME" not in tier:
            if action != "up":
                action, severity = "up", "med"
            reasons.append(f"Disco em **{iops:.0f} IOPS** — avalie tier NVMe para I/O intensivo")

        # ── Scale DOWN: clear underutilization — judged on the 24h AVERAGE, not
        # the current snapshot, so an off-peak demo doesn't suggest downsizing ──
        if (action == "ok" and cpu_down < 15 and conn_pct < 20
                and mem_pct < 50 and disk_pct < 50 and tier != "M10"):
            action, severity = "down", "low"
            reasons.append(f"CPU {'média 24h ' if cpu24 else ''}**{cpu_down}%**, memória **{mem_pct}%**, "
                           f"disco **{disk_pct}%** — tudo baixo, possível economia de custo")

        headlines = {
            "up":   "⬆️ Recomendação: Scale UP",
            "down": "⬇️ Oportunidade: Scale DOWN (economia)",
            "ok":   "✅ Tier adequado para a carga atual",
        }
        if action == "ok" and not reasons:
            reasons.append(f"{cpu_lbl} **{cpu_up}%**, memória **{mem_pct}%**, storage **{disk_pct}%** — "
                           "tudo saudável, nenhuma ação necessária")

        return {"action": action, "severity": severity,
                "reasons": reasons, "headline": headlines[action],
                # key metrics for the UI to display (CPU · Memory · Storage · Connections)
                "metrics": {
                    "cpu_pct": cpu, "mem_pct": mem_pct, "disk_pct": disk_pct,
                    "cpu_p95_24h": cpu24["p95"] if cpu24 else None,
                    "cpu_avg_24h": cpu24["avg"] if cpu24 else None,
                    "connections": conns, "conn_pct": round(conn_pct, 1),
                    "memory_used_gb": measurements.get("memory_used_gb", 0),
                    "mem_total_gb": measurements.get("mem_total_gb", 0),
                    "iops": round(iops),
                }}

    # ── Performance Advisor ───────────────────────────────────────────────
    def get_suggested_indexes(self, project_id, process_id):
        return self._get(
            f"/groups/{project_id}/processes/{process_id}/performanceAdvisor/suggestedIndexes"
        )

    def get_slow_queries(self, project_id, process_id):
        return self._get(
            f"/groups/{project_id}/processes/{process_id}/performanceAdvisor/slowQueryLogs"
        )

    # ── Alerts ────────────────────────────────────────────────────────────
    def get_open_alerts(self, project_id):
        try:
            return self._get(
                f"/groups/{project_id}/alerts", params={"status": "OPEN"}
            ).get("results", [])
        except Exception:
            logger.exception("get_open_alerts failed project_id=%s", project_id)
            return []

    # ── Invoice ───────────────────────────────────────────────────────────
    def get_pending_invoice(self):
        try:
            return self._get(f"/orgs/{self.org_id}/invoices/pending")
        except Exception:
            logger.exception("get_pending_invoice failed org_id=%s", self.org_id)
            return {}

    # ── Cost estimation (static, no API call) ─────────────────────────────
    @staticmethod
    def estimate_cost(tier: str, usd_brl: float = 5.70) -> dict:
        usd = TIER_PRICING_USD.get(tier, 0)
        return {
            "tier": tier,
            "usd": usd,
            "brl": round(usd * usd_brl),
        }


# ── Direct pymongo index creation (requires connection string) ─────────────
def create_index_direct(mongo_uri: str, namespace: str, index_keys: list) -> dict:
    """Creates an index directly via pymongo. Retries once on replica state change.

    Returns a dict {"result": str, "collection_stats": dict|None} so callers
    (and the frontend) can surface a "collection has N documents — build may
    take a while" warning before/after the operation.
    """
    try:
        from pymongo import MongoClient
        from pymongo.errors import OperationFailure
        parts     = namespace.split(".", 1)
        db_name   = parts[0]
        coll_name = parts[1] if len(parts) > 1 else parts[0]

        # Direction can be 1/-1 but also "hashed", "2dsphere", "text" — keep
        # non-numeric values as strings instead of crashing on int()
        def _direction(v):
            try:
                return int(v)
            except (TypeError, ValueError):
                return str(v)
        keys = [(list(k.keys())[0], _direction(list(k.values())[0])) for k in index_keys]
        mc        = MongoClient(mongo_uri, serverSelectionTimeoutMS=6000)

        try:
            # Best-effort collStats: lets the UI warn about build impact on
            # large collections before/after the index creation.
            coll_stats = None
            try:
                stats = mc[db_name].command("collStats", coll_name)
                coll_stats = {
                    "count": stats.get("count", 0),
                    "size_bytes": stats.get("size", 0),
                    "storage_size_bytes": stats.get("storageSize", 0),
                }
            except Exception:
                logger.exception("collStats failed namespace=%s", namespace)

            for attempt in range(2):
                try:
                    name = mc[db_name][coll_name].create_index(keys)
                    msg = f"✅ Índice criado: `{name}`"
                    if coll_stats and coll_stats["count"] > 100_000:
                        msg += (f"\n\n⚠️ Coleção com **{coll_stats['count']:,}** documentos — "
                                f"o build do índice pode levar algum tempo.")
                    return {"result": msg, "collection_stats": coll_stats}
                except OperationFailure as e:
                    if e.code == 11602 and attempt == 0:
                        # Primary election in progress — wait and retry once
                        import time; time.sleep(3)
                        continue
                    return {
                        "result": (
                            f"❌ Erro MongoDB ({e.code}): {e.details.get('errmsg', str(e))}\n\n"
                            f"**Dica:** O cluster pode estar em processo de eleição de primário. "
                            f"Aguarde ~30s e tente novamente."
                        ),
                        "collection_stats": coll_stats,
                    }
            return {"result": "❌ Falha após retry. Tente novamente em alguns instantes.",
                    "collection_stats": coll_stats}
        finally:
            mc.close()
    except ImportError:
        return {"result": "❌ pymongo não instalado. Execute: pip install pymongo",
                "collection_stats": None}
    except Exception as e:
        return {"result": f"❌ Erro de conexão: {e}", "collection_stats": None}
