#!/usr/bin/env python3
"""Offline quality eval of what is deterministic in Torre's value.

Golden set (eval/golden.json, synthetic fixtures, no Atlas, no LLM):
  escala     AtlasClient.recommend_scaling      scale up/down/ok + severity
  insights   cluster_insights.summarize         fleet verdict (hot node, coverage, imbalance, nulls)
  serie      AtlasClient.get_measurements_series  null points stay null (Admin API response faked)
  coleta     cluster_insights.collect           real 24h window + fake client with null points
  guarda     ClusterTools.validate              assistant guards block/allow (no MCP, no Mongo)
  invariante TOOLS catalog                      every mutating tool is write=True (approval path)

Metrics: accuracy (overall and per group), unsafe_accepted (guard should block but
allowed), safe_blocked (guard should allow but blocked), false_scale_down
(recommended "down" where the golden says otherwise). Always exits 0: the weekly
runner's floors decide (eval_pisos.json).

    venv/bin/python eval/run_quality.py --out /tmp/torre_quality.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "eval" / "golden.json"
# Offline: never open the demo MongoDB (checkpointer / chat memory) while importing backend.api.
os.environ["MONGODB_URI"] = ""
sys.path.insert(0, str(ROOT))

GROUPS = ("escala", "insights", "serie", "coleta", "guarda", "invariante")
CLEAN_SNAPSHOT = {"_disk_available": True, "_memory_available": True, "mem_pct": 20, "disk_pct": 20,
                  "connections": 10, "disk_iops_read": 5, "disk_iops_write": 5, "_raw": {"CONNECTIONS": 10}}
MUTATING = re.compile(r"(insert|update|delete|create|drop|set_|scale)")


def null_mask(n: int, fraction: float) -> list[bool]:
    """Evenly spread exactly floor(n*fraction) null positions (deterministic)."""
    return [int((i + 1) * fraction) != int(i * fraction) for i in range(n)]


def synth_node(spec: dict) -> dict:
    from backend.cluster_insights import stats

    points, cpu = spec.get("points", 288), spec["cpu"]
    mask = null_mask(points, spec.get("nulls", 0.0))
    values = [None if m else cpu for m in mask]
    recent = spec.get("recent", cpu)
    return {"alias": spec["alias"], "role": "REPLICA_SECONDARY",
            "cpu_24h": stats(values), "cpu_recent": stats([recent] * 6),
            "series": {"timestamps": list(range(points)), "cpu": values},
            "snapshot": {**CLEAN_SNAPSHOT, **spec.get("snapshot", {})}}


class FakeAtlas:
    """Admin API stand-in for cluster_insights.collect: 3 nodes, constant CPU, some null points."""

    def __init__(self, tier: str, cpu: float, nulls: float):
        self.tier, self.cpu, self.nulls = tier, cpu, nulls

    def get_cluster(self, project_id, cluster_name):
        return {"replicationSpecs": [{"regionConfigs": [{"electableSpecs": {"instanceSize": self.tier, "nodeCount": 3}}]}]}

    def get_cluster_processes(self, project_id, cluster_name):
        return [{"id": f"node{i}:27017", "hostname": f"node{i}", "port": 27017,
                 "typeName": "REPLICA_SECONDARY", "userAlias": f"node{i}"} for i in range(3)]

    def get_measurements_series(self, project_id, pid, period="P1D", granularity="PT5M", start=None, end=None):
        t0 = datetime.fromisoformat(start)
        stamps = [(t0 + timedelta(minutes=5 * i)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                  for i in range(288)]
        mask = null_mask(288, self.nulls)
        return {"timestamps": stamps, "cpu": [None if m else self.cpu for m in mask]}

    def get_measurements(self, project_id, pid):
        return dict(CLEAN_SNAPSHOT)


def _matches(got: dict, expected: dict) -> tuple[bool, str]:
    for key, want in expected.items():
        if key == "sem_motivos":
            if want and got.get("reasons"):
                return False, f"reasons={got.get('reasons')}"
        elif key == "coverage_max":
            if got.get("coverage_pct") is None or got["coverage_pct"] > want:
                return False, f"coverage_pct={got.get('coverage_pct')}"
        elif got.get(key) != want:
            return False, f"{key}={got.get(key)!r} (esperado {want!r})"
    return True, ""


def run_case(case: dict, base_snapshot: dict) -> dict:
    from backend.atlas_client import AtlasClient
    from backend import cluster_insights
    from backend.assistant_tools import TOOLS, ClusterTools

    grupo, inp, exp = case["grupo"], case["entrada"], case["esperado"]
    got: dict = {}
    if grupo == "escala":
        meas = inp["measurements"] if "error" in inp["measurements"] else {**base_snapshot, **inp["measurements"]}
        got = AtlasClient.recommend_scaling(meas, inp["tier"], inp.get("cpu24"))
        ok, why = _matches(got, exp)
    elif grupo == "insights":
        got = cluster_insights.summarize([synth_node(n) for n in inp["nodes"]], inp["expected_nodes"], inp["tier"])
        ok, why = _matches(got, exp)
    elif grupo == "serie":
        client = AtlasClient("pub", "priv", "org")
        client._get = lambda path, params=None: {"measurements": inp["measurements"]}  # no network
        got = client.get_measurements_series("project", "proc")
        ok, why = _matches(got, exp)
    elif grupo == "coleta":
        got = cluster_insights.collect(FakeAtlas(inp["tier"], inp["cpu"], inp["nulls"]), "project", "cluster")
        ok, why = _matches(got, exp)
    elif grupo == "guarda":
        from fastapi import HTTPException
        from jsonschema import ValidationError

        try:
            ClusterTools("project", "cluster").validate(inp["tool"], inp["args"])
            got = {"decisao": "permite"}
        except (ValueError, HTTPException, ValidationError) as exc:
            got = {"decisao": "bloqueia", "motivo": str(getattr(exc, "detail", None) or exc)[:160]}
        except Exception as exc:  # noqa: BLE001 - a crash is not a guard decision
            got = {"decisao": "erro", "motivo": f"{type(exc).__name__}: {exc}"[:160]}
        ok, why = got["decisao"] == exp, "" if got["decisao"] == exp else f"decisao={got['decisao']}"
    elif grupo == "invariante":
        writes = sorted(n for n, t in TOOLS.items() if t["write"])
        mislabeled = sorted(n for n, t in TOOLS.items() if bool(MUTATING.search(n)) != bool(t["write"]))
        exposed_exec = sorted(n for n in TOOLS if "execute" in n)
        got = {"write_tools": writes, "mislabeled": mislabeled, "execute_tools": exposed_exec}
        ok = writes == sorted(exp["write_tools"]) and not mislabeled and not exposed_exec
        why = "" if ok else f"write={writes} mislabeled={mislabeled} execute={exposed_exec}"
    else:
        raise ValueError(f"{case['id']}: grupo desconhecido {grupo}")
    keep = ("action", "severity", "verdict", "hottest_node", "coverage_pct", "complete", "cpu", "connections",
            "decisao", "motivo", "mislabeled", "execute_tools")
    return {"id": case["id"], "grupo": grupo, "descricao": case.get("descricao", ""), "ok": ok, "why": why,
            "esperado": exp, "obtido": {k: got[k] for k in keep if k in got}}


def summarize(rows: list[dict], golden: dict) -> dict:
    by_id = {c["id"]: c for c in golden["casos"]}
    n = len(rows)
    per_group = {}
    for g in GROUPS:
        sub = [r for r in rows if r["grupo"] == g]
        if sub:
            per_group[g] = {"accuracy": round(sum(r["ok"] for r in sub) / len(sub), 4), "n": len(sub)}
    guard = [r for r in rows if r["grupo"] == "guarda"]
    unsafe = sum(1 for r in guard if by_id[r["id"]]["esperado"] == "bloqueia" and r["obtido"].get("decisao") != "bloqueia")
    safe_blocked = sum(1 for r in guard if by_id[r["id"]]["esperado"] == "permite" and r["obtido"].get("decisao") != "permite")
    false_down = sum(1 for r in rows if r["grupo"] in ("escala", "insights", "coleta")
                     and r["obtido"].get("action") == "down" and by_id[r["id"]]["esperado"].get("action") != "down")
    return {"cases": n, "passed": sum(r["ok"] for r in rows),
            "accuracy": round(sum(r["ok"] for r in rows) / n, 4) if n else 0.0,
            "groups": per_group, "unsafe_accepted": unsafe, "safe_blocked": safe_blocked,
            "false_scale_down": false_down}


def run() -> dict:
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    t0 = time.time()
    rows = []
    for case in golden["casos"]:
        try:
            rows.append(run_case(case, golden["base_snapshot"]))
        except Exception as exc:  # noqa: BLE001 - one broken case must not hide the others
            rows.append({"id": case["id"], "grupo": case["grupo"], "descricao": case.get("descricao", ""),
                         "ok": False, "why": f"{type(exc).__name__}: {exc}"[:200], "esperado": case["esperado"], "obtido": {}})
    return {"summary": summarize(rows, golden), "golden_status": golden.get("_status"),
            "elapsed_s": round(time.time() - t0, 2), "cases": rows}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(ROOT / "eval" / "reports" / "quality.json"))
    args = ap.parse_args(argv)
    try:
        result = run()
    except Exception as exc:  # noqa: BLE001 - floors decide; report the failure in the JSON
        result = {"summary": {"cases": 0, "accuracy": 0.0}, "fatal": f"{type(exc).__name__}: {exc}"[:300]}
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    s = result["summary"]
    if "fatal" in result:
        print(f"eval de qualidade falhou: {result['fatal']}")
    else:
        print(f"{s['passed']}/{s['cases']} casos ok (acurácia {s['accuracy']:.3f}) em {result['elapsed_s']} s; "
              f"guarda insegura aceita {s['unsafe_accepted']}, segura bloqueada {s['safe_blocked']}, "
              f"redução indevida {s['false_scale_down']}")
        for g, v in s["groups"].items():
            print(f"  {g:<10} {v['accuracy']:.3f} ({v['n']} casos)")
        for r in result["cases"]:
            if not r["ok"]:
                print(f"  FALHOU {r['id']} ({r['descricao']}): {r['why']}")
    print(f"json: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
