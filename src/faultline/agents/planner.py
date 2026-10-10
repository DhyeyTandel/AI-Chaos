import json

from pydantic import BaseModel, Field, field_validator

from .. import baseline as bl
from .. import faults, llm

TEMPLATE_FAULT = {
    "crash": "is crashed",
    "delay": "gets added network latency",
    "loss": "suffers packet loss",
}


class Candidate(BaseModel):
    id: str
    fault: str
    kind: str
    target: str
    params: dict
    duration_s: int
    label: str


class Scored(BaseModel):
    candidate: Candidate
    det_score: float
    components: dict[str, float]
    llm_rank: int | None = None
    reason: str = ""


class RankedItem(BaseModel):
    candidate_id: str
    rank: int
    reason: str = Field(default="")

    @field_validator("reason", mode="before")
    @classmethod
    def _trim(cls, v):
        return str(v)[:200]


class PlannerOut(BaseModel):
    ranking: list[RankedItem]
    hypothesis: str
    predicted_blast_radius: list[str] = Field(default_factory=list)


class PlanResult(BaseModel):
    ranked: list[Scored]
    hypothesis: str
    blast_radius: list[str]
    fallback: bool
    fallback_reason: str | None = None
    model: str
    latency_ms: float = 0.0
    cached: bool = False


def load_map(cfg, overrides_path, out_path) -> dict:
    from pathlib import Path

    from .. import mapper

    p = Path(out_path)
    if p.is_file():
        return json.loads(p.read_text())
    return mapper.build_map(cfg, overrides_path, p)


def generate_candidates(cfg, graph: dict) -> list[Candidate]:
    p, s = cfg.planner, cfg.safety
    out = []
    for target, attrs in sorted(graph["nodes"].items()):
        if attrs.get("kind") not in ("app", "infra"):
            continue
        if target in faults.NEVER or target not in s.allow or target in p.exclude:
            continue
        if target not in s.crash_exclude:
            out.append(Candidate(
                id=f"crash:{target}:{p.crash_duration_s}s", fault="crash", kind="crash", target=target,
                params={}, duration_s=p.crash_duration_s, label=f"crash {p.crash_duration_s}s",
            ))
        for d in p.delay_ms:
            out.append(Candidate(
                id=f"delay:{target}:{d}ms", fault="delay", kind=f"delay{d}", target=target,
                params={"delay_ms": d, "jitter_ms": 0, "loss_pct": 0}, duration_s=p.duration_s, label=f"delay {d}ms",
            ))
        for pct in p.loss_pct:
            out.append(Candidate(
                id=f"loss:{target}:{pct}pct", fault="delay", kind="loss", target=target,
                params={"delay_ms": 1, "jitter_ms": 0, "loss_pct": pct}, duration_s=p.duration_s, label=f"loss {pct}%",
            ))
    return out


def history(store, limit: int) -> list[dict]:
    rows = store.conn.execute("SELECT * FROM experiments ORDER BY created_at DESC LIMIT ?", (limit,))
    out = []
    for r in rows:
        r = dict(r)
        r["params"] = json.loads(r["params"])
        r["impact"] = json.loads(r["impact"]) if r["impact"] else None
        out.append(r)
    return out


def is_degraded(e: dict) -> bool:
    if e["state"] == "ABORTED" and "watchdog" in (e.get("abort_reason") or ""):
        return True
    for v in (e.get("impact") or {}).values():
        er, be = v.get("error_rate"), v.get("base_error_rate")
        if er is not None and er - (be or 0.0) > 0.05:
            return True
        p95, bp = v.get("p95_ms"), v.get("base_p95_ms")
        if p95 is not None and bp and p95 > 1.5 * bp:
            return True
    return False


def neighbours(graph: dict) -> dict[str, set[str]]:
    nb: dict[str, set[str]] = {n: set() for n in graph["nodes"]}
    for e in graph["edges"]:
        nb.setdefault(e["source"], set()).add(e["target"])
        nb.setdefault(e["target"], set()).add(e["source"])
    return nb


def dependents(graph: dict, target: str) -> list[str]:
    kinds = {n: a.get("kind") for n, a in graph["nodes"].items()}
    return sorted({e["source"] for e in graph["edges"] if e["target"] == target and kinds.get(e["source"]) in ("app", "infra")})


def score_candidates(cfg, graph: dict, hist: list[dict], cands: list[Candidate]) -> list[Scored]:
    w, sev = cfg.planner.weights, cfg.planner.severity
    fan = {n: len({e["source"] for e in graph["edges"] if e["target"] == n}) for n in graph["nodes"]}
    max_fan = max(fan.values(), default=0) or 1
    journeys = graph.get("journeys") or {}
    nb = neighbours(graph)
    tested_pairs = {(e["fault"], e["target"], json.dumps(e["params"], sort_keys=True)) for e in hist}
    tested_targets = {e["target"] for e in hist}
    degraded_targets = {e["target"] for e in hist if is_degraded(e)}
    held_targets = {e["target"] for e in hist if e["state"] == "COMPLETED" and not is_degraded(e)} - degraded_targets
    out = []
    for c in cands:
        node = graph["nodes"].get(c.target, {})
        pj = {k: v for k, v in c.params.items() if k != "duration_s"}
        if c.fault == "crash":
            same = any(f == "crash" and t == c.target for f, t, _ in tested_pairs)
        else:
            same = any(
                f == c.fault and t == c.target and all(json.loads(pp).get(k) == v for k, v in pj.items())
                for f, t, pp in tested_pairs
            )
        untested = 0.0 if same else 0.5 if c.target in tested_targets else 1.0
        recent = 0.0
        if any(c.target in nb.get(d, ()) for d in degraded_targets if d != c.target):
            recent += 1.0
        if c.target in held_targets:
            recent -= 1.0
        comps = {
            "fan_in": fan.get(c.target, 0) / max_fan,
            "journeys_affected": (sum(1 for v in journeys.values() if c.target in v["services"]) / len(journeys)) if journeys else 0.0,
            "stateful": 1.0 if node.get("stateful") else 0.0,
            "untested": untested,
            "recent_result": recent,
            "severity": getattr(sev, c.kind, 0.5),
        }
        total = (
            w.fan_in * comps["fan_in"] + w.journeys * comps["journeys_affected"] + w.stateful * comps["stateful"]
            + w.untested * comps["untested"] + w.recent * comps["recent_result"] + w.severity * comps["severity"]
        )
        out.append(Scored(candidate=c, det_score=round(total, 3), components={k: round(v, 3) for k, v in comps.items()}))
    return out


def select_top(cfg, scored: list[Scored], top_k: int) -> list[Scored]:
    order = {c.id: i for i, c in enumerate(sorted((s.candidate for s in scored), key=lambda c: c.id))}
    ranked = sorted(scored, key=lambda s: (-s.det_score, order[s.candidate.id]))
    cap, per, top = cfg.planner.max_per_target, {}, []
    for s in ranked:
        if per.get(s.candidate.target, 0) >= cap:
            continue
        per[s.candidate.target] = per.get(s.candidate.target, 0) + 1
        top.append(s)
        if len(top) == top_k:
            break
    return top


def template_reason(s: Scored) -> str:
    c = s.components
    parts = [f"fan_in {c['fan_in']:.2f}", f"journeys {c['journeys_affected']:.2f}"]
    if c["stateful"]:
        parts.append("stateful")
    parts.append({1.0: "untested", 0.5: "target tested with another fault", 0.0: "already tested"}[c["untested"]])
    if c["recent_result"] > 0:
        parts.append("neighbour degraded earlier")
    elif c["recent_result"] < 0:
        parts.append("held up earlier")
    return ", ".join(parts)


def blast_radius(graph: dict, target: str) -> list[str]:
    js = [f"journey:{j}" for j, v in (graph.get("journeys") or {}).items() if target in v["services"]]
    return dependents(graph, target) + js


def template_hypothesis(graph: dict, c: Candidate) -> str:
    deps = dependents(graph, c.target)
    js = [j for j, v in (graph.get("journeys") or {}).items() if c.target in v["services"]]
    what = TEMPLATE_FAULT["loss" if c.kind == "loss" else c.fault]
    return (
        f"If {c.target} {what} ({c.label}), callers ({', '.join(deps[:6]) or 'none'}) will show errors or higher latency "
        f"and journeys ({', '.join(js) or 'none'}) will degrade."
    )


def build_context(cfg, graph: dict, store, top: list[Scored], hist: list[dict]) -> str:
    nodes = ",".join(n + ("*" if a.get("stateful") else "") for n, a in sorted(graph["nodes"].items()) if a.get("kind") in ("app", "infra"))
    edges = ",".join(f"{e['source']}>{e['target']}" for e in graph["edges"])
    journeys = "; ".join(f"{j}: {','.join(v['services'])}" for j, v in (graph.get("journeys") or {}).items())
    b = bl.latest(store)
    base = "none"
    if b:
        base = "; ".join(
            f"{j}: p95 {v['p95_ms']:.0f}ms err {100 * (v['error_rate'] or 0):.1f}%" for j, v in b["journeys"].items() if v.get("p95_ms") is not None
        )
    h = "; ".join(
        f"{e['fault']} {e['target']} {e['params'].get('delay_ms', '')} -> {e['state']}"
        f"{' degraded' if is_degraded(e) else ' held'}"
        for e in hist[:5]
    ) or "none"
    cand = "\n".join(
        f"- {s.candidate.id} (target {s.candidate.target}, {s.candidate.label}) det_score {s.det_score} {json.dumps(s.components)}" for s in top
    )
    return (
        f"Services (* stateful): {nodes}\nCalls (caller>callee): {edges}\nJourneys: {journeys}\n"
        f"Baseline: {base}\nLast experiments: {h}\nCandidates:\n{cand}"
    )


SYSTEM = (
    "You are the planner of a chaos engineering tool for a microservice demo shop. Rank the candidate fault experiments "
    "by how much resilience insight they give (hidden dependencies, single points of failure, untested paths). "
    "Use only the candidate ids given. Reply with one JSON object and nothing else, in this format: "
    '{"ranking":[{"candidate_id":"<id>","rank":1,"reason":"<=200 chars"}],'
    '"hypothesis":"what you expect to happen for the rank 1 pick","predicted_blast_radius":["service or journey names affected by the rank 1 pick"]}. '
    "Include every candidate exactly once."
)


def plan(cfg, graph: dict, store, top_k: int | None = None, chat=None) -> PlanResult:
    top_k = top_k or cfg.planner.top_k
    hist = history(store, cfg.planner.history)
    scored = score_candidates(cfg, graph, hist, generate_candidates(cfg, graph))
    top = select_top(cfg, scored, top_k)
    model = cfg.llm.planner_model
    if not top:
        return PlanResult(ranked=[], hypothesis="", blast_radius=[], fallback=True, fallback_reason="no candidates", model=model)
    ctx = build_context(cfg, graph, store, top, hist)
    out, meta = llm.call_structured(
        "planner", model, PlannerOut, SYSTEM, ctx, cfg.llm.timeout_s, store=store,
        input_summary=f"{len(top)} candidates, {len(graph['nodes'])} nodes, {len(hist)} past experiments", chat=chat,
    )
    by_id = {s.candidate.id: s for s in top}
    ordered, seen = [], set()
    if out is not None:
        for r in sorted(out.ranking, key=lambda r: r.rank):
            if r.candidate_id in by_id and r.candidate_id not in seen:
                seen.add(r.candidate_id)
                s = by_id[r.candidate_id]
                s.llm_rank, s.reason = len(ordered) + 1, r.reason
                ordered.append(s)
        if not ordered:
            out = None
            meta["error"] = "no valid candidate ids in output"
            store.add_trace("planner", model, "ranking validation", None, 0.0, True, meta["error"])
    fallback = out is None
    for s in top:
        if s.candidate.id not in seen:
            ordered.append(s)
    for s in ordered:
        if not s.reason:
            s.reason = template_reason(s)
    first = ordered[0].candidate
    if fallback:
        hyp, blast = template_hypothesis(graph, first), blast_radius(graph, first.target)
    else:
        known = set(graph["nodes"]) | {f"journey:{j}" for j in (graph.get("journeys") or {})} | set(graph.get("journeys") or {})
        blast = [b for b in out.predicted_blast_radius if b in known] or blast_radius(graph, first.target)
        hyp = out.hypothesis or template_hypothesis(graph, first)
    return PlanResult(
        ranked=ordered, hypothesis=hyp, blast_radius=blast, fallback=fallback, fallback_reason=meta["error"] if fallback else None,
        model=model, latency_ms=meta["latency_ms"], cached=meta["cached"],
    )


def hypothesis_for(res: PlanResult, graph: dict, pick: Scored) -> tuple[str, list[str]]:
    if pick is res.ranked[0]:
        return res.hypothesis, res.blast_radius
    return template_hypothesis(graph, pick.candidate), blast_radius(graph, pick.candidate.target)
