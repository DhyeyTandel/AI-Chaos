import json
import math
import re

from . import baseline as bl

SPACES = {"\u202f": " ", "\u00a0": " "}
HYPHENS = {"\u2013": "-", "\u2011": "-", "\u2012": "-", "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"'}
SYMBOLS = {
    "\u2192": "->", "\u21d2": "=>", "\u2190": "<-", "\u2194": "<->", "\u2264": "<=", "\u2265": ">=", "\u2260": "!=",
    "\u2248": "~", "\u00d7": "x", "\u2026": "...", "\u2022": "-", "\u00b7": "-", "\u2212": "-", "\u2009": " ", "\u200a": " ",
    "\u2002": " ", "\u2003": " ", "\u201a": ",", "\u201e": '"', "\u2032": "'", "\u2033": '"', "\u00b1": "+/-", "\u200b": "",
    "\u2713": "ok", "\u2714": "ok", "\u2717": "x", "\u2718": "x",
}


def clean(text: str) -> str:
    """Plain ASCII punctuation: em dashes become ' - ', non-breaking hyphens and en dashes '-', arrows and other common symbols
    their ASCII spelling, odd spaces ' '."""
    text = re.sub(r"[ \t]*[\u2014\u2015][ \t]*", " - ", text)
    for k, v in {**SPACES, **HYPHENS, **SYMBOLS}.items():
        text = text.replace(k, v)
    return text


def clean_deep(obj):
    if isinstance(obj, str):
        return clean(obj)
    if isinstance(obj, list):
        return [clean_deep(x) for x in obj]
    if isinstance(obj, dict):
        return {clean(k) if isinstance(k, str) else k: clean_deep(v) for k, v in obj.items()}
    return obj


def clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def fault_kind(fault: str, params: dict) -> str:
    if fault == "crash":
        return "crash"
    return "loss" if (params or {}).get("loss_pct", 0) else "delay"


def fault_label(fault: str, params: dict) -> str:
    k = fault_kind(fault, params)
    if k == "crash":
        return "crash"
    if k == "loss":
        return f"loss {params.get('loss_pct', 0):g}%"
    return f"delay {params.get('delay_ms', 0):g}ms"


def journey_degradation(cfg, v: dict) -> float:
    """0..1. max(error part, latency part). Error part: rise in error rate over baseline divided by err_full.
    Latency part: log(p95 / max(base p95, floor)) / log(p95_full_ratio), so 1x is 0 and p95_full_ratio x is 1."""
    s = cfg.score
    err = 0.0
    if v.get("error_rate") is not None:
        err = clamp((v["error_rate"] - (v.get("base_error_rate") or 0.0)) / s.err_full)
    lat = 0.0
    if v.get("p95_ms") is not None and v.get("base_p95_ms") is not None:
        base = max(v["base_p95_ms"], s.p95_base_floor_ms)
        lat = clamp(math.log(max(v["p95_ms"], 1e-9) / base) / math.log(s.p95_full_ratio))
    return max(err, lat)


def breached(v: dict, th: dict | None) -> bool:
    if not th:
        return False
    if v.get("error_rate") is not None and v["error_rate"] > th["error_rate_max"]:
        return True
    return v.get("p95_ms") is not None and v["p95_ms"] > th["p95_max_ms"]


def predicted_journeys(graph: dict, target: str, blast: list[str]) -> tuple[set[str], set[str]]:
    """(prediction, map): journeys the hypothesis names, journeys the map says depend on the target."""
    known = set((graph.get("journeys") or {}))
    mapped = {j for j, v in (graph.get("journeys") or {}).items() if target in v["services"]}
    named = {b.split(":", 1)[1] if b.startswith("journey:") else b for b in blast or []} & known
    return (named or mapped), mapped


def verdict(predicted: set[str], actual: set[str]) -> str:
    if predicted == actual:
        return "CONFIRMED"
    return "PARTIAL" if predicted & actual else "REFUTED"


def recovery_measured(exp: dict) -> bool:
    """False when an operator stop ended the experiment before recovery could be measured. Rows written before the
    recovery_measured column existed are judged by their abort source."""
    flag = exp.get("recovery_measured")
    if flag is not None:
        return bool(flag)
    return not (exp.get("recovery_seconds") is None and exp.get("abort_source") == "operator")


def recovery_norm(cfg, injected: bool, reverted: bool, recovery_s: float | None, measured: bool = True) -> tuple[float, bool]:
    """(component 0..1, never recovered). An unmeasured recovery contributes 0 and is never reported as never recovered."""
    if not injected or not reverted or not measured:
        return 0.0, False
    if recovery_s is None:
        return 1.0, True
    return min(1.0, recovery_s / cfg.score.recovery_cap_s), False


def penalty(cfg, impact: float, breadth: float, rec: float) -> float:
    s = cfg.score
    return 100.0 * (s.impact_weight * impact + s.breadth_weight * breadth + s.recovery_weight * rec)


def severity_of(p: float) -> str:
    return "critical" if p >= 60 else "high" if p >= 40 else "medium" if p >= 20 else "low"


def analyze_experiment(cfg, exp: dict, rnd: dict | None, graph: dict, thresholds: dict | None) -> dict:
    params = exp["params"]
    blast = (rnd or {}).get("predicted_blast_radius") or []
    pred, mapped = predicted_journeys(graph, exp["target"], blast)
    impact_map = exp.get("impact") or {}
    injected = exp.get("injected_at") is not None
    deg = {j: journey_degradation(cfg, v) for j, v in impact_map.items()}
    affected = sorted(
        j for j, v in impact_map.items()
        if deg[j] >= cfg.score.affected_margin or breached(v, (thresholds or {}).get(j))
    )
    total = len(impact_map) or len(graph.get("journeys") or {}) or 1
    impact = max(deg.values(), default=0.0)
    breadth = len(affected) / total
    measured = recovery_measured(exp)
    rec, never = recovery_norm(cfg, injected, exp.get("reverted_at") is not None, exp.get("recovery_seconds"), measured)
    pen = penalty(cfg, impact, breadth, rec) if injected else 0.0
    hidden = sorted(j for j in affected if j not in pred | mapped and deg[j] >= cfg.score.hidden_min_impact)
    return {
        "experiment_id": exp["id"], "round": (rnd or {}).get("round"), "scope": "run" if rnd else "history",
        "tier": (rnd or {}).get("tier"), "candidate_id": (rnd or {}).get("candidate_id"),
        "fault": exp["fault"], "kind": fault_kind(exp["fault"], params), "label": fault_label(exp["fault"], params),
        "target": exp["target"], "reason": (rnd or {}).get("reason"), "planner_fallback": bool((rnd or {}).get("fallback")),
        "hypothesis": (rnd or {}).get("hypothesis"), "predicted_blast_radius": blast,
        "predicted_journeys": sorted(pred), "mapped_journeys": sorted(mapped),
        "outcome": exp["state"], "abort_reason": exp.get("abort_reason"), "injected": injected,
        "degradation": {j: round(d, 3) for j, d in deg.items()}, "affected_journeys": affected,
        "impact": round(impact, 3), "breadth": round(breadth, 3), "recovery_s": exp.get("recovery_seconds"),
        "recovery_norm": round(rec, 3), "never_recovered": never, "recovery_measured": measured or not injected, "penalty": round(pen, 1),
        "verdict": verdict(pred, set(affected)) if injected else "n/a",
        "hidden_journeys": hidden,
        "abort_source": exp.get("abort_source"), "safety_decisions": exp.get("safety_decisions") or [],
        "preflight": exp.get("preflight_review"),
    }


def load_experiments(store, run_id: str, rounds: list[dict], until: float | None, history_limit: int = 50) -> list[tuple[dict, dict | None]]:
    def fetch(where, args):
        out = []
        for r in store.conn.execute(f"SELECT * FROM experiments WHERE {where}", args):
            r = dict(r)
            r["params"] = json.loads(r["params"])
            r["impact"] = json.loads(r["impact"]) if r["impact"] else None
            r["safety_decisions"] = json.loads(r["safety_decisions"]) if r.get("safety_decisions") else []
            r["preflight_review"] = json.loads(r["preflight_review"]) if r.get("preflight_review") else None
            out.append(r)
        return out

    pairs, ids = [], set()
    for rnd in rounds:
        rows = fetch("id = ?", (rnd["experiment_id"],)) if rnd.get("experiment_id") else []
        if rows:
            pairs.append((rows[0], rnd))
            ids.add(rows[0]["id"])
    hist = fetch("created_at <= ? ORDER BY created_at DESC LIMIT ?", (until or 1e18, history_limit + len(ids)))
    pairs += [(r, None) for r in reversed(hist) if r["id"] not in ids and r["injected_at"] is not None][-history_limit:]
    return pairs


def service_weight(graph: dict, target: str, items: list[dict]) -> int:
    mapped = sum(1 for v in (graph.get("journeys") or {}).values() if target in v["services"])
    seen = max((len(a["affected_journeys"]) for a in items), default=0)
    return max(1, mapped, seen)


def roll_up(cfg, graph: dict, analyzed: list[dict]) -> dict:
    by: dict[str, list[dict]] = {}
    for a in analyzed:
        if a["injected"]:
            by.setdefault(a["target"], []).append(a)
    services = {}
    for t, items in sorted(by.items()):
        worst = max(items, key=lambda a: a["penalty"])
        services[t] = {
            "score": round(100 - worst["penalty"], 1), "worst_experiment": worst["experiment_id"], "worst_label": worst["label"],
            "experiments": len(items), "weight": service_weight(graph, t, items),
        }
    wsum = sum(s["weight"] for s in services.values())
    overall = round(sum(s["score"] * s["weight"] for s in services.values()) / wsum, 1) if wsum else None
    nodes = {n for n, a in graph["nodes"].items() if a.get("kind") in ("app", "infra")}
    untested = sorted((nodes & set(cfg.safety.allow)) - set(services))
    return {"services": services, "overall": overall, "untested": untested}


def hidden_dependencies(graph: dict, analyzed: list[dict]) -> list[dict]:
    out = []
    for a in analyzed:
        if not a["hidden_journeys"]:
            continue
        mapped = a["mapped_journeys"]
        out.append({
            "experiment_id": a["experiment_id"], "scope": a["scope"], "target": a["target"], "fault": a["label"],
            "unexpected_journeys": a["hidden_journeys"],
            "worst_degradation": max(a["degradation"][j] for j in a["hidden_journeys"]),
            "map_note": f"map lists journeys {', '.join(mapped)}" if mapped else "map shows no journey depending on it",
        })
    return sorted(out, key=lambda h: -h["worst_degradation"])


def analyze(cfg, store, graph: dict, run: dict, rounds: list[dict]) -> dict:
    base = bl.latest(store)
    thresholds = base["thresholds"] if base else None
    pairs = load_experiments(store, run["run_id"], rounds, run.get("ended_at"))
    analyzed = [analyze_experiment(cfg, e, r, graph, thresholds) for e, r in pairs]
    roll = roll_up(cfg, graph, analyzed)
    return {
        "run": run, "experiments": analyzed, "hidden": hidden_dependencies(graph, analyzed),
        "journeys": sorted((graph.get("journeys") or {}).keys()), **roll,
    }


def formula_text(cfg) -> str:
    s = cfg.score
    return (
        f"penalty = 100 x ({s.impact_weight} x impact + {s.breadth_weight} x breadth + {s.recovery_weight} x recovery); "
        f"service score = 100 - worst penalty on that service; overall = mean of service scores weighted by the number of "
        f"journeys the service sits on (at least 1; the larger of the map's count and the journeys it was seen to degrade). "
        f"An experiment whose recovery was not measured (operator stop) contributes no recovery component."
    )
