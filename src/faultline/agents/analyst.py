import json
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from .. import analyzer, llm

Severity = Literal["critical", "high", "medium", "low"]


LIMITS = {"title": 120, "explanation": 400, "evidence": 300, "executive_summary": 600}


class Weakness(BaseModel):
    rank: int
    title: str
    severity: Severity
    services: list[str] = Field(default_factory=list)
    explanation: str
    evidence: str = ""
    related_experiment_ids: list[str] = Field(default_factory=list)
    source: str = "llm"

    @field_validator("title", "explanation", "evidence", mode="before")
    @classmethod
    def _trim(cls, v, info):
        return str(v)[: LIMITS[info.field_name]]

    @field_validator("severity", mode="before")
    @classmethod
    def _sev(cls, v):
        return str(v).strip().lower()


class AnalystOut(BaseModel):
    weaknesses: list[Weakness]
    executive_summary: str

    @field_validator("executive_summary", mode="before")
    @classmethod
    def _trim(cls, v):
        return analyzer.clean(str(v))[: LIMITS["executive_summary"]]


class AnalystResult(BaseModel):
    weaknesses: list[Weakness]
    executive_summary: str
    fallback: bool
    fallback_reason: str | None = None
    model: str
    dropped: int = 0


SYSTEM = (
    "You are the analyst of a chaos engineering tool for a microservice demo shop. You get measured results of fault "
    "experiments (computed deterministically; do not recompute scores). Rank the real weaknesses the results reveal: "
    "single points of failure, hidden dependencies (journeys degraded although the map did not predict it), slow or missing "
    "recovery, wide blast radius. Use only experiment ids and service names that appear in the input. Reply with one JSON object "
    "and nothing else, in this format: "
    '{"weaknesses":[{"rank":1,"title":"short","severity":"critical|high|medium|low","services":["svc"],'
    '"explanation":"<=400 chars, cause and impact","evidence":"<=300 chars, concrete numbers from the input",'
    '"related_experiment_ids":["exp-id"]}],"executive_summary":"<=600 chars for a manager"}. '
    "At most 5 weaknesses, most severe first. Recovery times at or near the stated measurement floor are normal, not a weakness, "
    "and no recovery target exists beyond what the input states. Use plain ASCII punctuation, no em dashes."
)


def compact(analysis: dict, limit: int = 14, floor_s: float = 5.0) -> str:
    rows = sorted((a for a in analysis["experiments"] if a["injected"]), key=lambda a: -a["penalty"])[:limit]
    lines = []
    for a in rows:
        lines.append(
            f"{a['experiment_id']} {a['label']} on {a['target']} ({a['scope']}) outcome {a['outcome']}"
            f"{' abort: ' + a['abort_reason'][:80] if a['abort_reason'] else ''}; predicted journeys {','.join(a['predicted_journeys']) or 'none'}; "
            f"degraded {','.join(a['affected_journeys']) or 'none'}; impact {a['impact']:.2f} breadth {a['breadth']:.2f} "
            f"recovery {'never' if a['never_recovered'] else a['recovery_s'] if a['recovery_s'] is not None else '-'}; "
            f"penalty {a['penalty']:.0f}; verdict {a['verdict']}"
            f"{'; HIDDEN dependency on ' + ','.join(a['hidden_journeys']) if a['hidden_journeys'] else ''}"
        )
    svc = "; ".join(f"{t} {v['score']:.0f}" for t, v in analysis["services"].items())
    return (
        f"Overall resilience score: {analysis['overall']}\nRecovery measurement floor: {floor_s:g}s (needs that many probe samples back inside baseline)\nService scores: {svc or 'none'}\n"
        f"Untested services: {', '.join(analysis['untested']) or 'none'}\nExperiments (worst first):\n" + "\n".join(lines)
    )


def validate(out: AnalystOut, exp_ids: set[str], services: set[str], max_n: int) -> tuple[list[Weakness], int]:
    good, dropped = [], 0
    for w in sorted(out.weaknesses, key=lambda w: w.rank):
        if not w.related_experiment_ids or any(i not in exp_ids for i in w.related_experiment_ids) or any(s not in services for s in w.services):
            dropped += 1
            continue
        good.append(w)
    good = good[:max_n]
    for i, w in enumerate(good, 1):
        w.rank = i
    return good, dropped


def fallback_weaknesses(cfg, analysis: dict) -> list[Weakness]:
    rows = [a for a in analysis["experiments"] if a["injected"] and a["penalty"] >= cfg.score.weak_min_penalty]
    best: dict[str, dict] = {}
    for a in sorted(rows, key=lambda a: -a["penalty"]):
        best.setdefault(a["target"], a)
    out = []
    for i, a in enumerate(sorted(best.values(), key=lambda a: -a["penalty"])[: cfg.score.max_weaknesses], 1):
        rec = "never recovered" if a["never_recovered"] else f"recovered in {a['recovery_s']:.1f}s" if a["recovery_s"] is not None else "recovery not measured"
        hidden = f" Unpredicted impact on {', '.join(a['hidden_journeys'])}: a hidden dependency." if a["hidden_journeys"] else ""
        out.append(Weakness(
            rank=i, title=f"{a['target']} is a weak point under {a['label']}", severity=analyzer.severity_of(a["penalty"]),
            services=[a["target"]],
            explanation=f"{a['label']} on {a['target']} degraded {', '.join(a['affected_journeys']) or 'no journey'} (impact {a['impact']:.2f}, breadth {a['breadth']:.2f}); {rec}.{hidden}",
            evidence=f"{a['experiment_id']}: penalty {a['penalty']:.0f}, outcome {a['outcome']}, verdict {a['verdict']}",
            related_experiment_ids=[a["experiment_id"]], source="fallback",
        ))
    return out


def fallback_summary(analysis: dict, ws: list[Weakness]) -> str:
    n = sum(1 for a in analysis["experiments"] if a["injected"])
    if not ws:
        return f"{n} experiments measured; none showed a material weakness. Overall resilience score {analysis['overall']}."
    return (
        f"{n} experiments measured, overall resilience score {analysis['overall']}. "
        f"Top weakness: {ws[0].title} ({ws[0].severity}). {len(analysis['hidden'])} hidden dependency finding(s)."
    )


def analyze(cfg, store, analysis: dict, known_services: set[str], chat=None) -> AnalystResult:
    model = cfg.llm.analyst_model
    exp_ids = {a["experiment_id"] for a in analysis["experiments"]}
    services = known_services | {a["target"] for a in analysis["experiments"]}
    out, meta = None, {"error": "no injected experiments"}
    if any(a["injected"] for a in analysis["experiments"]):
        out, meta = llm.call_structured(
            "analyst", model, AnalystOut, SYSTEM, compact(analysis, floor_s=cfg.recovery.min_samples * 1.0), cfg.llm.timeout_s, store=store,
            input_summary=f"{len(analysis['experiments'])} analyzed experiments, overall {analysis['overall']}", chat=chat,
        )
    dropped = 0
    if out is not None:
        good, dropped = validate(out, exp_ids, services, cfg.score.max_weaknesses)
        if good:
            return AnalystResult(weaknesses=good, executive_summary=out.executive_summary, fallback=False, model=model, dropped=dropped)
        meta["error"] = "no valid weaknesses in output"
        store.add_trace("analyst", model, "weakness validation", None, 0.0, True, meta["error"])
    ws = fallback_weaknesses(cfg, analysis)
    return AnalystResult(
        weaknesses=ws, executive_summary=fallback_summary(analysis, ws), fallback=True, fallback_reason=meta["error"],
        model=model, dropped=dropped,
    )
