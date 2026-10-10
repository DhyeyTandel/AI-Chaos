import json

from pydantic import BaseModel, Field, field_validator

from .. import analyzer
from .. import baseline as bl
from .. import faults, llm, memory
from ..mapper import INFRA, TOOLING

NOISE = INFRA | TOOLING | {"flagd-ui", "grafana", "opensearch"}

TEMPLATE_FAULT = {
    "crash": "is crashed",
    "delay": "gets added network latency",
    "loss": "suffers packet loss",
    "spike": "faces a traffic surge",
}


class Candidate(BaseModel):
    id: str
    fault: str
    kind: str
    target: str
    params: dict
    duration_s: int
    label: str
    tier: int = 1


class Scored(BaseModel):
    candidate: Candidate
    det_score: float
    components: dict[str, float]
    llm_rank: int | None = None
    reason: str = ""


class RankedItem(BaseModel):
    candidate_id: str = ""
    index: int | None = None
    rank: int
    reason: str = Field(default="")

    @field_validator("candidate_id", mode="before")
    @classmethod
    def _id(cls, v):
        return "" if v is None else str(v).strip()

    @field_validator("index", mode="before")
    @classmethod
    def _index(cls, v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    @field_validator("reason", mode="before")
    @classmethod
    def _trim(cls, v):
        return analyzer.clean(str(v))[:200]


class PlannerOut(BaseModel):
    ranking: list[RankedItem]
    hypothesis: str
    predicted_blast_radius: list[str] = Field(default_factory=list)

    @field_validator("hypothesis", mode="before")
    @classmethod
    def _clean(cls, v):
        return analyzer.clean(str(v))


class PlanResult(BaseModel):
    ranked: list[Scored]
    hypothesis: str
    blast_radius: list[str]
    fallback: bool
    fallback_reason: str | None = None
    model: str
    latency_ms: float = 0.0
    cached: bool = False
    exclusions: list[dict] = Field(default_factory=list)
    limits: list[dict] = Field(default_factory=list)


def load_map(cfg, overrides_path, out_path) -> dict:
    from pathlib import Path

    from .. import mapper

    p = Path(out_path)
    if p.is_file():
        return json.loads(p.read_text())
    return mapper.build_map(cfg, overrides_path, p)


def assign_tier(cfg, graph: dict, c: Candidate) -> int:
    t = cfg.planner.tiers
    over = vars(t.overrides).get(c.id)
    if over:
        return int(over)
    if c.fault == "spike":
        return int(cfg.planner.spike_tier)
    if c.fault == "crash":
        journeys = graph.get("journeys") or {}
        hit = sum(1 for v in journeys.values() if c.target in v["services"])
        if graph["nodes"].get(c.target, {}).get("stateful") or c.target in t.critical or (journeys and hit == len(journeys)):
            return 3
        return 2 if hit else 1
    if c.kind == "loss":
        return 2
    return 1 if c.params.get("delay_ms", 0) <= t.light_delay_ms else 2


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
    for u in p.spike_users:
        out.append(Candidate(
            id=f"spike:{faults.TRAFFIC}:{u}users", fault="spike", kind="spike", target=faults.TRAFFIC,
            params={"users": u, "spawn_rate": p.spike_spawn_rate}, duration_s=p.spike_duration_s, label=f"spike {u} users",
        ))
    for c in out:
        c.tier = assign_tier(cfg, graph, c)
    return out


def history(store, limit: int) -> list[dict]:
    rows = store.conn.execute("SELECT * FROM experiments WHERE state != 'VETOED' ORDER BY created_at DESC LIMIT ?", (limit,))
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
            "journeys_affected": (len(analyzer.mapped_journeys(graph, c.target)) / len(journeys)) if journeys else 0.0,
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


def journey_callers(graph: dict, target: str) -> list[str]:
    """Direct callers of target that sit on a journey through target; telemetry and tooling edges are ignored."""
    on = {s for v in (graph.get("journeys") or {}).values() if target in v["services"] for s in v["services"]}
    return [d for d in dependents(graph, target) if d not in NOISE and d in on]


def blast_radius(graph: dict, target: str) -> list[str]:
    js = [f"journey:{j}" for j in sorted(analyzer.mapped_journeys(graph, target))]
    return journey_callers(graph, target) + js


def template_hypothesis(graph: dict, c: Candidate) -> str:
    deps = journey_callers(graph, c.target)
    js = sorted(analyzer.mapped_journeys(graph, c.target))
    what = TEMPLATE_FAULT["loss" if c.kind == "loss" else c.fault]
    head = f"If {c.target} {what} ({c.label})" if c.fault != "spike" else f"If the shop {what} ({c.label})"
    if not js:
        return f"{head}, no user journey depends on it; expect no journey to degrade."
    callers = f"callers on those journeys ({', '.join(deps[:6])}) will show errors or higher latency and " if deps else ""
    return f"{head}, {callers}journeys ({', '.join(js)}) will degrade."


def build_context(cfg, graph: dict, store, top: list[Scored], hist: list[dict], strategy: str | None = None, limits: list[dict] | None = None) -> str:
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
        f"{i}. {s.candidate.id} (target {s.candidate.target}, {s.candidate.label}{f', tier {s.candidate.tier}' if strategy else ''}) "
        f"det_score {s.det_score} {json.dumps(s.components)}" for i, s in enumerate(top, 1)
    )
    out = (
        f"Services (* stateful): {nodes}\nCalls (caller>callee): {edges}\nJourneys: {journeys}\n"
        f"Baseline: {base}\nLast experiments: {h}\n"
    )
    if limits:
        out += f"Known limits (aborted by watchdog or safety agent in earlier runs, lowest intensity): {memory.prompt_text(limits)}\n"
    if strategy:
        out += f"Strategy: {strategy}\n"
    return out + f"Candidates:\n{cand}"


SYSTEM = (
    "You are the planner of a chaos engineering tool for a microservice demo shop. Rank the candidate fault experiments "
    "by how much resilience insight they give (hidden dependencies, single points of failure, untested paths). "
    "The candidates are a numbered list. Refer to each one by its list number as index AND by its exact id copied from the list, "
    "never by an id you made up. Reply with one JSON object and nothing else, in this format: "
    '{"ranking":[{"index":1,"candidate_id":"<id>","rank":1,"reason":"<=200 chars"}],'
    '"hypothesis":"what you expect to happen for the rank 1 pick","predicted_blast_radius":["service or journey names affected by the rank 1 pick"]}. '
    "Include every candidate exactly once."
)
LIMITS_NOTE = (
    " The user message lists known limits: fault intensities that earlier runs aborted. Candidates at or above them were removed. "
    "When a pick sits close to a limit, say so in its reason."
)
RUN_NOTE = (
    " The user message gives the run strategy (survivable first, climb as results come in). "
    "Let your reasons reflect it: say why the pick fits the current tier and what the earlier results suggest."
)


def resolve(r: RankedItem, top: list[Scored], by_id: dict[str, Scored]) -> Scored | None:
    """The candidate a ranking row names: its exact id, else its list number (also when the id field holds a number)."""
    if r.candidate_id in by_id:
        return by_id[r.candidate_id]
    hit = [i for i in by_id if i in r.candidate_id] if r.candidate_id else []
    if len(hit) == 1:
        return by_id[hit[0]]
    num = r.index
    if num is None and r.candidate_id.rstrip(".").isdigit():
        num = int(r.candidate_id.rstrip("."))
    return top[num - 1] if num is not None and 1 <= num <= len(top) else None


def order_ranking(out: PlannerOut | None, top: list[Scored], by_id: dict[str, Scored]) -> tuple[list[Scored], set[str]]:
    ordered, seen = [], set()
    if out is None:
        return ordered, seen
    for r in sorted(out.ranking, key=lambda r: r.rank):
        s = resolve(r, top, by_id)
        if s is not None and s.candidate.id not in seen:
            seen.add(s.candidate.id)
            s.llm_rank, s.reason = len(ordered) + 1, r.reason
            ordered.append(s)
    return ordered, seen


def plan(cfg, graph: dict, store, top_k: int | None = None, chat=None, max_tier: int | None = None,
         exclude=(), strategy: str | None = None) -> PlanResult:
    top_k = top_k or cfg.planner.top_k
    hist = history(store, cfg.planner.history)
    cands = [c for c in generate_candidates(cfg, graph) if (max_tier is None or c.tier <= max_tier) and c.target not in exclude]
    limits = memory.known_limits(cfg, store)
    cands, excluded = memory.exclusions(cands, limits)
    scored = score_candidates(cfg, graph, hist, cands)
    top = select_top(cfg, scored, top_k)
    model = cfg.llm.planner_model
    if not top:
        return PlanResult(
            ranked=[], hypothesis="", blast_radius=[], fallback=True, fallback_reason="no candidates", model=model,
            exclusions=excluded, limits=limits,
        )
    ctx = build_context(cfg, graph, store, top, hist, strategy, limits)
    system = SYSTEM + (RUN_NOTE if strategy else "") + (LIMITS_NOTE if limits else "")
    summary = f"{len(top)} candidates, {len(graph['nodes'])} nodes, {len(hist)} past experiments"
    out, meta = llm.call_structured("planner", model, PlannerOut, system, ctx, cfg.llm.timeout_s, store=store, input_summary=summary, chat=chat)
    by_id = {s.candidate.id: s for s in top}
    ordered, seen = order_ranking(out, top, by_id)
    if out is not None and not ordered:
        store.add_trace("planner", model, "ranking validation", None, 0.0, True, "no valid candidate ids in output")
        if meta.get("cache_key"):
            store.cache_drop(meta["cache_key"])
        bad = ", ".join(sorted({r.candidate_id for r in out.ranking if r.candidate_id})[:5]) or "none"
        fix = (
            f"\nYour previous answer used ids that are not in the list ({bad}). Valid ids: {', '.join(by_id)}. "
            "Answer again with the same JSON format, giving each candidate's list number as index and its exact id."
        )
        out, meta = llm.call_structured(
            "planner", model, PlannerOut, system, ctx + fix, cfg.llm.timeout_s, store=store, input_summary=summary + " (id retry)", chat=chat, cache=False,
        )
        ordered, seen = order_ranking(out, top, by_id)
    if out is not None and not ordered:
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
        model=model, latency_ms=meta["latency_ms"], cached=meta["cached"], exclusions=excluded, limits=limits,
    )


def hypothesis_for(res: PlanResult, graph: dict, pick: Scored) -> tuple[str, list[str]]:
    if pick is res.ranked[0]:
        return res.hypothesis, res.blast_radius
    return template_hypothesis(graph, pick.candidate), blast_radius(graph, pick.candidate.target)
