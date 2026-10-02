"""Evidence shared by FinOps, Scale, reports and the read-only MCP tool."""
import math
from atlas_client import AtlasClient
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta


def stats(values):
    values = sorted(v for v in values if isinstance(v, (int, float)) and math.isfinite(v))
    if not values:
        return None
    return {"avg": round(sum(values) / len(values), 1),
            "p95": round(values[max(0, math.ceil(.95 * len(values)) - 1)], 1),
            "max": round(values[-1], 1), "samples": len(values)}


def summarize(nodes, expected_nodes, tier):
    """Equal-weight nodes in common five-minute UTC buckets; hottest node governs pressure."""
    available = [n for n in nodes if n.get("cpu_24h")]
    maps = [{t: v for t, v in zip(n.get("series", {}).get("timestamps", []), n.get("series", {}).get("cpu", []))
             if v is not None} for n in nodes]
    common = set.intersection(*(set(m) for m in maps)) if maps else set()
    fleet = stats([sum(m[t] for m in maps) / len(maps) for t in sorted(common)])
    coverage = min(100, round(len(common) / 288 * 100, 1))
    recent_complete = bool(nodes) and all((n.get("cpu_recent") or {}).get("samples", 0) >= 3 for n in nodes)
    complete = len(nodes) >= expected_nodes and coverage >= 90 and recent_complete
    hottest = max(available, key=lambda n: n["cpu_24h"]["p95"], default=None)
    worst = hottest["cpu_24h"]["p95"] if hottest else None
    recent = max((n["cpu_recent"]["p95"] for n in nodes if n.get("cpu_recent")), default=None)
    averages = [n["cpu_24h"]["avg"] for n in available]
    imbalance = round(max(averages) - min(averages), 1) if len(averages) > 1 else None
    reasons = []
    action, color, verdict = "unknown", "muted", "histórico insuficiente"
    if not available:
        reasons.append("Nenhuma série de CPU válida; não é possível avaliar utilização.")
    else:
        if not complete:
            reasons.append(f"Cobertura simultânea de 24h: {coverage}% · {len(available)}/{expected_nodes} nós com CPU. Dados recentes suficientes: {'sim' if recent_complete else 'não'}. Redução de tier bloqueada.")
        if worst >= 75 or (recent is not None and recent >= 80):
            action, color, verdict = "up", "red", "pressão de CPU — avaliar scale up / queries"
            reasons.append(f"Maior p95 por nó: {worst}% em 24h; maior p95 nos últimos 30 min: {recent}%.")
        elif imbalance is not None and imbalance >= 20:
            action, color, verdict = "investigate", "yellow", "carga desigual — investigar nó"
        elif complete:
            action, color, verdict = "ok", "green", "CPU sem saturação — validar demais recursos"
            if all(n["cpu_24h"]["avg"] < 15 and n["cpu_24h"]["p95"] < 40 for n in nodes) and tier != "M10":
                snapshots = [n.get("snapshot", {}) for n in nodes]
                limit = AtlasClient.TIER_CONN_LIMIT.get(tier, 1500)
                if all(m.get("_disk_available") and m.get("_memory_available")
                       and m.get("disk_pct", 100) < 50 and m.get("mem_pct", 100) < 50
                       and "CONNECTIONS" in m.get("_raw", {}) and m.get("connections", limit) < limit * .2
                       and m.get("disk_iops_read", 3000) + m.get("disk_iops_write", 3000) < 1000
                       for m in snapshots) and (recent is None or recent < 40):
                    action, color, verdict = "down", "yellow", "candidato a scale down — validar tier alvo"
                    reasons.append("CPU baixa em todos os nós e folga no snapshot de RAM, disco, I/O e conexões. Validar working set, limites do tier alvo e sazonalidade antes de reduzir.")
        if imbalance is not None and imbalance >= 20:
            reasons.append(f"Diferença entre médias dos nós: {imbalance} p.p. Correlacionar papel do nó, read preference, queries e replicação; assimetria não prova falha.")
    reasons.append("Memória usada isoladamente não prova pressão de cache. Métricas recentes são snapshots; 24h não demonstra sazonalidade semanal.")
    public_nodes = [{k: v for k, v in n.items() if k != "series"} for n in nodes]
    return {"nodes": public_nodes, "expected_nodes": expected_nodes, "measured_nodes": len(available),
            "coverage_pct": coverage, "recent_complete": recent_complete, "complete": complete, "cpu": fleet["avg"] if fleet else None,
            "cpu_p95": fleet["p95"] if fleet else None, "worst_node_p95": worst,
            "hottest_node": hottest["alias"] if hottest else None,
            "recent_p95": recent, "imbalance_pp": imbalance, "action": action,
            "color": color, "verdict": verdict, "reasons": reasons,
            "aggregation": "equal-weight node mean in shared 5-minute UTC buckets",
            "window": "P1D", "granularity": "PT5M", "recent_window_minutes": 30}


def collect(client, project_id, cluster_name, tier=None):
    try:
        cluster = client.get_cluster(project_id, cluster_name)
    except Exception:
        return {**summarize([], 3, tier or "Free/Shared"), "status": "unavailable",
                "reasons": ["Não foi possível obter configuração do cluster; verifique acesso Atlas."]}
    if not tier:
        try:
            tier = cluster["replicationSpecs"][0]["regionConfigs"][0]["electableSpecs"]["instanceSize"]
        except (KeyError, IndexError, TypeError):
            tier = "Free/Shared"
    if cluster.get("paused"):
        return {**summarize([], 0, tier), "status": "paused", "verdict": "pausado — sem utilização", "reasons": ["Cluster pausado; não há processos ativos para avaliar."]}
    try:
        processes = client.get_cluster_processes(project_id, cluster_name)
    except Exception:
        return {**summarize([], 3, tier), "status": "unavailable",
                "reasons": ["Não foi possível identificar processos do cluster; verifique acesso Atlas."]}
    expected = sum((rc.get(kind) or {}).get("nodeCount", 0)
                   for spec in cluster.get("replicationSpecs", []) for rc in spec.get("regionConfigs", [])
                   for kind in ("electableSpecs", "readOnlySpecs", "analyticsSpecs")) or 3
    end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    end = end.replace(minute=(end.minute // 5) * 5)
    start = end - timedelta(days=1)
    cutoff = end - timedelta(minutes=30)

    def node(p):
        pid = p.get("id") or f"{p['hostname']}:{p['port']}"
        out = {"process_id": pid, "alias": p.get("userAlias") or p["hostname"],
               "role": p.get("typeName"), "replica_set": p.get("replicaSetName"), "last_ping": p.get("lastPing")}
        try:
            series = client.get_measurements_series(project_id, pid, period="P1D", granularity="PT5M",
                                                    start=start.isoformat(), end=end.isoformat())
            # Atlas collectors can report seconds apart even with identical bounds.
            # Compare fixed five-minute UTC buckets, never list positions.
            buckets = {}
            for t, v in zip(series.get("timestamps", []), series.get("cpu", [])):
                instant = datetime.fromisoformat(t.replace("Z", "+00:00"))
                bucket = instant.replace(minute=(instant.minute // 5) * 5, second=0, microsecond=0)
                if start <= bucket < end and v is not None:
                    buckets[bucket.isoformat()] = v
            series["timestamps"] = sorted(buckets)
            series["cpu"] = [buckets[t] for t in series["timestamps"]]
            snapshot = client.get_measurements(project_id, pid)
            recent = []
            for t, v in zip(series.get("timestamps", []), series.get("cpu", [])):
                if datetime.fromisoformat(t.replace("Z", "+00:00")) >= cutoff:
                    recent.append(v)
            cpu = stats(series.get("cpu", []))
            out.update(series=series, cpu_24h=cpu, cpu_recent=stats(recent), snapshot=snapshot,
                       coverage_pct=min(100, round((cpu or {}).get("samples", 0) / 288 * 100, 1)),
                       error=series.get("error") or snapshot.get("error"))
        except Exception:
            out.update(cpu_24h=None, cpu_recent=None, snapshot={}, error="Coleta Atlas indisponível para este nó.")
        return out

    with ThreadPoolExecutor(max_workers=3) as pool:
        nodes = list(pool.map(node, processes))
    result = summarize(nodes, expected, tier)
    result.update(status="evaluated" if result["complete"] else "partial", tier=tier,
                  collected_at=datetime.now(timezone.utc).isoformat())
    return result
