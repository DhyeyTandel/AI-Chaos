import re
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator

from .. import llm
from .analyst import Weakness

ALLOWED_SERVICE_KEYS = {"restart", "healthcheck", "deploy"}
RESTART = re.compile(r"^(no|always|unless-stopped|on-failure(:\d+)?)$")
LIMITS = {"fix_title": 120, "steps": 500, "expected_effect": 250}


class Fix(BaseModel):
    weakness_rank: int
    fix_title: str
    fix_type: Literal["config", "code", "architecture"]
    steps: str
    compose_override: str | None = None
    expected_effect: str = ""
    source: str = "llm"
    override_dropped: str | None = None

    @field_validator("fix_title", "steps", "expected_effect", mode="before")
    @classmethod
    def _trim(cls, v, info):
        return str(v)[: LIMITS[info.field_name]]

    @field_validator("fix_type", mode="before")
    @classmethod
    def _type(cls, v):
        return str(v).strip().lower()

    @field_validator("compose_override", mode="before")
    @classmethod
    def _yaml(cls, v):
        if isinstance(v, (dict, list)):
            return yaml.safe_dump(v, sort_keys=False)
        return v or None


class FixerOut(BaseModel):
    fixes: list[Fix] = Field(default_factory=list)


class FixerResult(BaseModel):
    fixes: list[Fix]
    fallback: bool
    fallback_reason: str | None = None
    model: str
    llm_fixes: int = 0
    filled: int = 0


SYSTEM = (
    "You are the fixer of a chaos engineering tool for a microservice demo shop run with Docker Compose. For each weakness "
    "propose one concrete, realistic fix. Proposals are never applied automatically. Reply with one JSON object and nothing "
    "else, in this format: "
    '{"fixes":[{"weakness_rank":1,"fix_title":"short","fix_type":"config|code|architecture","steps":"<=500 chars, concrete steps",'
    '"compose_override":"optional YAML","expected_effect":"<=250 chars"}]}. '
    "compose_override, when present, is a compose YAML string shaped like services:\\n  <name>:\\n    restart: unless-stopped, using only the "
    "keys restart, healthcheck and deploy.resources (limits, reservations). Omit it for code or architecture fixes. "
    "The faults are injected container crashes, network delay and packet loss; favour timeouts, retries, circuit breakers, "
    "caching, replicas and restart policies, and do not claim a healthcheck cures latency. One fix per weakness. "
    "Use plain ASCII punctuation, no em dashes."
)


def check_override(text: str, services: set[str], default_svc: str | None = None) -> tuple[str | None, str | None]:
    """Returns (normalised yaml, None) when valid, else (None, reason). A bare service body is wrapped under default_svc."""
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        return None, "not valid YAML"
    if default_svc and isinstance(doc, dict) and doc and set(doc) <= ALLOWED_SERVICE_KEYS:
        doc = {"services": {default_svc: doc}}
    if not isinstance(doc, dict) or set(doc) != {"services"} or not isinstance(doc["services"], dict) or not doc["services"]:
        return None, "top level must be services"
    for name, svc in doc["services"].items():
        if name not in services:
            return None, f"unknown service {name}"
        if not isinstance(svc, dict) or not svc or set(svc) - ALLOWED_SERVICE_KEYS:
            return None, f"{name}: only restart, healthcheck, deploy.resources allowed"
        if "restart" in svc and not RESTART.match(str(svc["restart"])):
            return None, f"{name}: bad restart policy"
        if "healthcheck" in svc and not isinstance(svc["healthcheck"], dict):
            return None, f"{name}: healthcheck must be a mapping"
        if "deploy" in svc:
            d = svc["deploy"]
            if not isinstance(d, dict) or set(d) != {"resources"} or not isinstance(d["resources"], dict) or set(d["resources"]) - {"limits", "reservations"}:
                return None, f"{name}: deploy may only contain resources.limits and resources.reservations"
    return yaml.safe_dump(doc, sort_keys=False), None


def role_of(graph: dict, svc: str) -> str:
    if svc in ("flagd",):
        return "config"
    if svc in ("frontend", "frontend-proxy"):
        return "entry"
    if svc == "kafka":
        return "broker"
    if graph["nodes"].get(svc, {}).get("stateful"):
        return "store"
    return "service"


def fallback_fix(graph: dict, w: Weakness, kind: str) -> Fix:
    svc = w.services[0] if w.services else "the service"
    role = role_of(graph, svc) if w.services else "service"
    restart = f"services:\n  {svc}:\n    restart: unless-stopped\n"
    if kind == "crash":
        steps = {
            "store": f"Run {svc} with a persistent volume and a healthcheck, add a replica or standby, and make callers retry with backoff and fail fast while it restarts.",
            "broker": f"Give {svc} a healthcheck, keep topics replicated or persisted, and make producers buffer and retry so a broker restart does not fail user requests.",
            "entry": f"Run two replicas of {svc} behind the proxy and keep restart: unless-stopped so a crash is absorbed quickly.",
            "config": f"Let callers cache the last known flag values and default safely when {svc} is unreachable; keep restart: unless-stopped.",
            "service": f"Keep restart: unless-stopped, add a healthcheck, and wrap calls to {svc} in a timeout, retry and circuit breaker with a safe default.",
        }[role]
        return Fix(
            weakness_rank=w.rank, fix_title=f"Absorb a {svc} crash", fix_type="architecture" if role in ("store", "broker", "entry") else "config",
            steps=steps, compose_override=restart, expected_effect="Faster recovery and fewer failed requests while the service restarts.", source="fallback",
        )
    steps = (
        f"Set a short client timeout and a circuit breaker on calls to {svc}, add a bounded retry with jitter, and serve a cached or "
        f"degraded response when it is slow, so latency does not propagate up the journey."
    )
    return Fix(
        weakness_rank=w.rank, fix_title=f"Contain {svc} latency", fix_type="code", steps=steps, compose_override=None,
        expected_effect="Journey p95 stays near baseline while the dependency is slow or lossy.", source="fallback",
    )


def context(analysis: dict, graph: dict, weaknesses: list[Weakness]) -> str:
    by_id = {a["experiment_id"]: a for a in analysis["experiments"]}
    lines = []
    for w in weaknesses:
        exps = [by_id[i] for i in w.related_experiment_ids if i in by_id]
        faults = ",".join(sorted({f"{e['label']} on {e['target']}" for e in exps}))
        roles = ",".join(f"{s}({role_of(graph, s)})" for s in w.services)
        lines.append(f"{w.rank}. [{w.severity}] {w.title}. Services {roles}. Faults {faults}. {w.explanation} Evidence: {w.evidence}")
    return "Weaknesses:\n" + "\n".join(lines)


def fix(cfg, store, analysis: dict, graph: dict, weaknesses: list[Weakness], chat=None) -> FixerResult:
    model = cfg.llm.fixer_model
    if not weaknesses:
        return FixerResult(fixes=[], fallback=True, fallback_reason="no weaknesses", model=model)
    services = set(graph["nodes"])
    by_id = {a["experiment_id"]: a for a in analysis["experiments"]}
    out, meta = llm.call_structured(
        "fixer", model, FixerOut, SYSTEM, context(analysis, graph, weaknesses), cfg.llm.timeout_s, store=store,
        input_summary=f"{len(weaknesses)} weaknesses", chat=chat,
    )
    ranks = {w.rank: w for w in weaknesses}
    fixes: dict[int, Fix] = {}
    if out is not None:
        for f in out.fixes:
            if f.weakness_rank in ranks and f.weakness_rank not in fixes and f.steps.strip():
                if f.compose_override:
                    wsv = ranks[f.weakness_rank].services
                    f.compose_override, f.override_dropped = check_override(f.compose_override, services, wsv[0] if wsv else None)
                fixes[f.weakness_rank] = f
        if not fixes:
            meta["error"] = "no valid fixes in output"
            store.add_trace("fixer", model, "fix validation", None, 0.0, True, meta["error"])
    llm_n, filled = len(fixes), 0
    for w in weaknesses:
        if w.rank not in fixes:
            kinds = [by_id[i]["kind"] for i in w.related_experiment_ids if i in by_id]
            fixes[w.rank] = fallback_fix(graph, w, "crash" if "crash" in kinds else "delay")
            filled += 1
    return FixerResult(
        fixes=[fixes[r] for r in sorted(fixes)], fallback=llm_n == 0, fallback_reason=meta["error"] if llm_n == 0 else None,
        model=model, llm_fixes=llm_n, filled=filled,
    )
