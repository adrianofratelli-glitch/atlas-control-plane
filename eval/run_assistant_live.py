#!/usr/bin/env python3
"""Live eval of the assistant's ANSWERS (real Atlas + Claude via Grove + real MCP).

run_quality.py measures what is deterministic (heuristic, guards). This one runs
the real turn (backend.assistant_runtime.run_assistant) and checks the answer and
the tool trajectory with deterministic rules (no LLM judge):

  must_call        tools that must be called
  forbid_call      tools that must not be called
  actions          "none" (no approval card) | "pending_only" (cards allowed, none executed)
                   | "pending:<tool>" (exactly that operation prepared, pending)
  contains_any     at least one of these fragments (case/accent-insensitive)
  forbid_regex     none of these patterns in the answer
  grounded_cpu     at least one "NN%" in the answer within 1.5 pp of a CPU figure
                   computed independently from cluster_insights (same Admin API data)
  forbid_target_dbs  the answer must not name databases that exist on the
                   MONGODB_URI cluster (case run against ANOTHER cluster of the project)

Roles instead of names (the repo is public): "target" = the cluster MONGODB_URI
points to (backend.cluster_identity), "other" = another cluster of the same
project, "paused" = a paused cluster. Cases whose role is missing are skipped.

Safety: only reads and *prepares* actions. Nothing is ever approved: the eval
has no approval path. Checkpoints go to MONGODB_DB (must end in _test, default
torre_test); approvals to a temporary SQLite. Costs Grove tokens (~6 turns).

    venv/bin/python eval/run_assistant_live.py --out /tmp/torre_assistant_live.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import tempfile
import time
import unicodedata
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "eval" / "golden_assistant.json"
sys.path.insert(0, str(ROOT))


def norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in text if not unicodedata.combining(c)).lower()


PCT = re.compile(r"(\d{1,3}(?:[.,]\d+)?)\s?%")


def percents(text: str) -> list[float]:
    return [float(m.replace(",", ".")) for m in PCT.findall(text or "")]


def cpu_figures(insights: dict) -> list[float]:
    """Every CPU number the assistant could legitimately quote (cluster + per node)."""
    out = []
    for key in ("cpu", "cpu_p95", "worst_node_p95", "recent_p95"):
        if isinstance(insights.get(key), (int, float)):
            out.append(float(insights[key]))
    for node in insights.get("nodes") or []:
        for window in ("cpu_24h", "cpu_recent"):
            for stat in ("avg", "p95", "max"):
                v = (node.get(window) or {}).get(stat)
                if isinstance(v, (int, float)):
                    out.append(float(v))
    return out


def check_case(case: dict, run: dict, ctx: dict) -> list[str]:
    """Pure: returns the list of failed rules (empty = pass)."""
    fails = []
    answer, tools, actions = run["answer"], set(run["tools"]), run["actions"]
    if run.get("error"):
        fails.append(f"erro no turno: {run['error']}")
    for t in case.get("must_call", []):
        if t not in tools:
            fails.append(f"não chamou {t}")
    for t in case.get("forbid_call", []):
        if t in tools:
            fails.append(f"chamou {t}")
    if any(a.get("state") not in ("pending", "expired") for a in actions):
        fails.append("ação saiu de pending sem aprovação")
    rule = case.get("actions", "none")
    if rule == "none" and actions:
        fails.append(f"preparou {len(actions)} ação(ões)")
    elif rule.startswith("pending:"):
        wanted = rule.split(":", 1)[1]
        ops = [a.get("operation") for a in actions]
        if ops != [wanted]:
            fails.append(f"esperava 1 cartão {wanted}, veio {ops}")
    n = norm(answer)
    if case.get("contains_any") and not any(norm(f) in n for f in case["contains_any"]):
        fails.append(f"sem nenhum de {case['contains_any']}")
    for pattern in case.get("forbid_regex", []):
        if re.search(pattern, n):
            fails.append(f"casou proibido /{pattern}/")
    if case.get("grounded_cpu"):
        figures = ctx.get("cpu_figures") or []
        cited = percents(answer)
        if not figures:
            fails.append("sem métrica de referência (cluster sem dados?)")
        elif not any(abs(c - f) <= 1.5 for c in cited for f in figures):
            fails.append(f"nenhum % citado ({cited[:6]}) bate com as métricas reais")
    if case.get("forbid_target_dbs"):
        leaked = [d for d in ctx.get("target_dbs", []) if re.search(rf"\b{re.escape(norm(d))}\b", n)]
        if leaked:
            fails.append(f"citou {len(leaked)} banco(s) do cluster do MONGODB_URI (ex.: {leaked[:2]})")
    return fails


def discover() -> dict:
    """Roles -> (project_id, cluster_name); figures used by the checks. Read-only."""
    from backend import api
    from backend.cluster_identity import uri_targets_cluster

    uri = os.getenv("MONGODB_URI", "")
    client = api.get_client()
    roles, ctx = {}, {}
    for proj in client.get_projects():
        for c in client.get_clusters(proj["id"]):
            key = (proj["id"], c["name"])
            if c.get("paused"):
                roles.setdefault("paused", key)
            if uri and uri_targets_cluster(uri, client.get_cluster(*key)):
                roles.setdefault("target", key)
    if "target" in roles:
        pid = roles["target"][0]
        for c in client.get_clusters(pid):
            if c["name"] != roles["target"][1]:
                roles.setdefault("other", (pid, c["name"]))
                break
        ins = api.collect_insights(client, *roles["target"])
        ctx["cpu_figures"] = cpu_figures(ins)
        from pymongo import MongoClient
        mc = MongoClient(uri, serverSelectionTimeoutMS=10000)
        ctx["target_dbs"] = [d for d in mc.list_database_names()
                             if d not in ("admin", "local", "config") and len(d) >= 4]
        mc.close()
    return {"roles": roles, "ctx": ctx}


async def run_turn(prompt: str, project_id: str, cluster_name: str, timeout: float) -> dict:
    from backend.assistant_runtime import run_assistant

    session = "eval" + uuid.uuid4().hex + uuid.uuid4().hex[:8]
    out = {"answer": "", "tools": [], "actions": [], "error": None}
    t0 = time.perf_counter()

    async def consume():
        async for ev in run_assistant([{"role": "user", "content": prompt}], project_id, cluster_name, session):
            kind = ev.get("type")
            if kind == "text":
                out["answer"] += ev.get("text", "")
            elif kind == "tool_start":
                out["tools"].append(ev["name"])
            elif kind == "action":
                out["actions"].append(ev["action"])
            elif kind == "error":
                out["error"] = ev.get("message")
    try:
        await asyncio.wait_for(consume(), timeout)
    except Exception as e:  # infra (Grove/Atlas/MCP) is reported, not hidden
        out["error"] = f"{type(e).__name__}: {e}"[:300]
    out["latency_s"] = round(time.perf_counter() - t0, 1)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="/tmp/torre_assistant_live.json")
    ap.add_argument("--only", default="", help="ids separados por vírgula")
    ap.add_argument("--timeout", type=float, default=180)
    args = ap.parse_args(argv)

    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    os.environ.setdefault("MONGODB_DB", "torre_test")
    if not os.environ["MONGODB_DB"].endswith("_test"):
        print("MONGODB_DB precisa terminar em _test (checkpoints do eval).", file=sys.stderr)
        return 2
    os.environ["TORRE_ACTION_DB"] = str(Path(tempfile.mkdtemp(prefix="torre-eval-")) / "actions.sqlite3")
    if not (os.getenv("GROVE_BASE_URL") and os.getenv("GROVE_API_KEY")):
        print("Grove não configurado: eval de respostas exige o gateway.", file=sys.stderr)
        return 2

    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    only = {s for s in args.only.split(",") if s}
    found = discover()
    roles, ctx = found["roles"], found["ctx"]
    results = []
    for case in golden["casos"]:
        if only and case["id"] not in only:
            continue
        key = roles.get(case["role"])
        if not key:
            results.append({"id": case["id"], "status": "skipped", "reason": f"sem cluster no papel {case['role']}"})
            print(f"  -    {case['id']}: sem cluster '{case['role']}'")
            continue
        run = asyncio.run(run_turn(case["prompt"], key[0], key[1], args.timeout))
        fails = check_case(case, run, ctx)
        status = "pass" if not fails else "fail"
        results.append({"id": case["id"], "role": case["role"], "status": status, "fails": fails,
                        "tools": run["tools"], "actions": [a.get("operation") for a in run["actions"]],
                        "latency_s": run["latency_s"], "error": run["error"], "answer": run["answer"][:1500]})
        print(f"  {'ok ' if status == 'pass' else 'X  '}  {case['id']} ({run['latency_s']}s) {'; '.join(fails)}")
    ran = [r for r in results if r["status"] != "skipped"]
    passed = sum(r["status"] == "pass" for r in ran)
    summary = {"cases": len(ran), "passed": passed, "skipped": len(results) - len(ran),
               "accuracy": round(passed / len(ran), 3) if ran else None,
               "infra_errors": sum(1 for r in ran if r.get("error")),
               "unapproved_executions": sum(1 for r in ran if "ação saiu de pending sem aprovação" in r.get("fails", []))}
    print(f"\nrespostas: {passed}/{len(ran)} (pulados {summary['skipped']}, erros de infra {summary['infra_errors']})")
    Path(args.out).write_text(json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    print(f"json: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
