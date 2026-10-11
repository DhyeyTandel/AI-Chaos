import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, field_validator, model_validator

from .. import analyzer, faults, llm, watchdog
from ..injector import Injector
from ..ledger import Ledger
from ..store import Store
from . import safety

AGENT = "recovery"
MODEL = "deterministic"
MENU = ("reapply_revert", "restart_target", "restart_service", "kill_switch", "escalate")
STATEFUL = frozenset({"astronomy-db", "valkey-cart", "kafka"})
DIAGNOSIS_MAX = 300
REASON_MAX = 200
WINDOW_S = 10.0


class RecoveryOut(BaseModel):
    diagnosis: str
    action: Literal["reapply_revert", "restart_target", "restart_service", "kill_switch", "escalate"]
    service: str | None = None
    reason: str

    @field_validator("action", mode="before")
    @classmethod
    def _lower(cls, v):
        return str(v).strip().lower()

    @field_validator("service", mode="before")
    @classmethod
    def _service(cls, v):
        v = None if v is None else str(v).strip()
        return v or None

    @field_validator("diagnosis", mode="before")
    @classmethod
    def _diag(cls, v):
        return analyzer.clean(str(v))[:DIAGNOSIS_MAX]

    @field_validator("reason", mode="before")
    @classmethod
    def _reason(cls, v):
        return analyzer.clean(str(v))[:REASON_MAX]

    @model_validator(mode="after")
    def _needs_service(self):
        if self.action == "restart_service" and not self.service:
            raise ValueError("restart_service needs a service")
        return self


SYSTEM = (
    "You are the recovery agent of a chaos engineering tool on a microservice demo shop. An experiment's fault has already been "
    "reverted by deterministic code, but the user journeys did not return to their baseline band within the recovery window. "
    "You get the journeys still degraded (measured against baseline and limits), whether each of this experiment's own reverts "
    "verifies, the state of the experiment's target and of the services on the degraded journeys, and recent Docker events. "
    "Diagnose the most likely cause and choose ONE action from this menu: "
    "reapply_revert (run the experiment's own revert again), restart_target (restart the experiment's own target container), "
    "restart_service (restart one named service from the candidate list, which is the only set allowed), "
    "kill_switch (revert everything and stop the run; use when a fault may still be active), "
    "escalate (do nothing and ask the operator; use when no menu action fits or the evidence is unclear). "
    "Prefer restart_service for a candidate that is not running, unhealthy or restarting, and escalate when nothing points at a candidate. "
    "Reply with one JSON object and nothing else: "
    '{"diagnosis":"<=300 chars, cite numbers or states from the input","action":"reapply_revert|restart_target|restart_service|kill_switch|escalate",'
    '"service":"<service name, required for restart_service, else null>","reason":"<=200 chars"}. '
    "Use plain ASCII punctuation, no em dashes."
)


@dataclass
class Ctx:
    cfg: object
    store: object
    driver: object
    exp: dict
    graph: dict | None
    thresholds: dict
    base: dict
    t_revert: float
    stop: asyncio.Event
    stop_path: object
    t_inject: float | None = None
    chat: object = None
    log: object = print
    actions: list = field(default_factory=list)

    @property
    def db_path(self):
        return self.store.path


def own_parts(exp: dict) -> list[dict]:
    p = exp["params"]
    return list(p.get("parts") or []) if exp["fault"] == faults.COMBO else [{"fault": exp["fault"], "target": exp["target"], "params": p}]


def own_targets(exp: dict) -> list[str]:
    return [p["target"] for p in own_parts(exp) if analyzer.fault_kind(p["fault"], p["params"]) in ("crash", "delay", "loss")]


def degraded_journeys(ctx, now: float) -> list[dict]:
    rows = ctx.store.query(since=now - 2 * WINDOW_S)
    out = []
    for j, th in ctx.thresholds.items():
        cur = safety.window_stats(rows, j, now - WINDOW_S, now)
        b = ctx.base["journeys"][j]
        if cur["count"] == 0 or analyzer.breached(cur, th):
            out.append({"journey": j, **cur, "base_error_rate": b["error_rate"], "base_p95_ms": b["p95_ms"], "limits": th})
    return out


def journey_services(ctx, journeys: list[str]) -> list[str]:
    js = (ctx.graph or {}).get("journeys") or {}
    seen: list[str] = []
    for j in journeys:
        for s in (js.get(j) or {}).get("services") or []:
            if s not in seen:
                seen.append(s)
    return seen


def candidates(ctx, journeys: list[str]) -> list[str]:
    own = set(own_targets(ctx.exp))
    return [
        s for s in journey_services(ctx, journeys)
        if s in ctx.cfg.safety.allow and s not in faults.NEVER and s != faults.TRAFFIC and (s not in STATEFUL or s in own)
    ]


def check_action(ctx, out: RecoveryOut, degraded: list[str]) -> tuple[bool, str]:
    """Guardrails in code: the menu is closed (RecoveryOut), a service restart is confined to allowlisted app services on a degraded journey."""
    if out.action == "restart_service":
        s = out.service
        if s in faults.NEVER or s not in ctx.cfg.safety.allow or s == faults.TRAFFIC:
            return False, f"{s} is not an allowlisted application service"
        if s in STATEFUL and s not in own_targets(ctx.exp):
            return False, f"{s} is a stateful data store and not this experiment's target"
        if s not in journey_services(ctx, degraded):
            return False, f"{s} is not on a degraded journey"
    if out.action == "restart_target" and not own_targets(ctx.exp):
        return False, "this experiment has no container target"
    return True, "ok"


def fmt_pct(v):
    return "-" if v is None else f"{100 * v:.0f}%"


def fmt_ms(v):
    return "-" if v is None else f"{v:.0f}ms"


def gather(ctx, deg: list[dict]) -> dict:
    """Blocking: Docker and ledger reads for the prompt."""
    names = list(dict.fromkeys([*own_targets(ctx.exp), *journey_services(ctx, [d["journey"] for d in deg])]))[:15]
    reverts = []
    store = Store(ctx.db_path)
    try:
        for e in Ledger(store).all():
            if e["experiment_id"] != ctx.exp["id"]:
                continue
            try:
                ok, detail = ctx.driver.check_reverted(e)
            except Exception as ex:
                ok, detail = False, f"{type(ex).__name__}: {str(ex)[:60]}"
            reverts.append(f"#{e['id']} {analyzer.part_label({'fault': e['kind'], 'target': e['target'], 'params': e['params']})}: "
                           f"ledger {e['state']}, {'verified' if ok else 'NOT verified'} ({detail})")
    finally:
        store.close()
    states = ctx.driver.container_states(names)
    events = ctx.driver.recent_events(names, ctx.t_revert - 120)
    return {"names": names, "reverts": reverts, "states": states, "events": events}


def build_input(ctx, deg: list[dict], info: dict, now: float) -> str:
    e = ctx.exp
    label = analyzer.fault_label(e["fault"], e["params"])
    lines = [
        f"Experiment {e['id']}: {label} on {e['target']}, reverted {now - ctx.t_revert:.0f}s ago; the journeys are not back in the baseline band.",
        "Journeys still degraded (last 10s):",
    ]
    for d in deg:
        n = d["count"]
        cur = "no samples" if n == 0 else f"n={n} err {fmt_pct(d['error_rate'])} p95 {fmt_ms(d['p95_ms'])}"
        lines.append(f"- {d['journey']}: {cur}; baseline err {fmt_pct(d['base_error_rate'])} p95 {fmt_ms(d['base_p95_ms'])}; "
                     f"limits err {fmt_pct(d['limits']['error_rate_max'])} p95 {fmt_ms(d['limits']['p95_max_ms'])}")
    lines.append("Revert verification (this experiment's ledger entries):")
    lines += [f"- {r}" for r in info["reverts"]] or ["- none"]
    lines.append("Containers (target and services on degraded journeys):")
    lines += [f"- {s['name']}: {s['state']} health {s['health']} restarts {s['restarts']}{' OOMKilled' if s['oom'] else ''}" for s in info["states"]]
    lines.append("Recent Docker events: " + ("; ".join(info["events"]) or "none"))
    cand = candidates(ctx, [d["journey"] for d in deg])
    lines.append("restart_service candidates: " + (", ".join(cand) or "none"))
    lines.append("Own container targets (restart_target): " + (", ".join(own_targets(e)) or "none"))
    if ctx.actions:
        lines.append("Already done: " + "; ".join(f"{a['action']} {a['service'] or ''} -> {a['result']}".replace("  ", " ") for a in ctx.actions))
    return "\n".join(lines)


def diagnose(ctx, text: str):
    store = Store(ctx.db_path)
    try:
        return llm.call_structured(
            AGENT, ctx.cfg.llm.recovery_model, RecoveryOut, SYSTEM, text, timeout_s=ctx.cfg.recovery_agent.timeout_s, store=store,
            input_summary=f"{ctx.exp['id']} recovery failed, step {len(ctx.actions) + 1}", chat=ctx.chat, cache=False,
        )
    finally:
        store.close()


def execute(ctx, out: RecoveryOut) -> str:
    st = Store(ctx.db_path)
    inj = Injector(Ledger(st), ctx.driver, ctx.cfg)
    try:
        if out.action == "reapply_revert":
            r = inj.reapply_revert(ctx.exp["id"])
            return "reapplied: " + (", ".join(f"#{i} {s}" for i, s in r.items()) or "no entries")
        if out.action == "restart_target":
            names = own_targets(ctx.exp)
            for n in names:
                ctx.driver.restart_container(n)
            return "restarted " + ", ".join(names)
        if out.action == "restart_service":
            ctx.driver.restart_container(out.service)
            return f"restarted {out.service}"
        if out.action == "kill_switch":
            ctx.stop_path.parent.mkdir(parents=True, exist_ok=True)
            ctx.stop_path.write_text(str(time.time()))
            r = inj.revert_all()
            return f"kill switch: reverted {r or 'nothing open'}; STOP sentinel left in place"
        return "no action"
    except Exception as e:
        return f"failed: {type(e).__name__}: {str(e)[:100]}"
    finally:
        st.close()


def record(ctx, step: int, action: str, service, reason: str, diagnosis: str, result: str):
    ctx.store.add_recovery_action(ctx.exp["id"], step, action, service, reason, diagnosis, result)
    ctx.store.add_trace(AGENT, MODEL, f"{ctx.exp['id']} action {action} {service or ''}".strip(), json.dumps({"result": result}), 0.0, False)
    ctx.actions.append({"step": step, "action": action, "service": service, "reason": reason, "diagnosis": diagnosis, "result": result})


def operator_message(why: str) -> str:
    return (f"Recovery agent: ESCALATED, no further automatic action. The system did not return to baseline ({why}). "
            "Operator: check `faultline status` and `docker ps`, read the failing container's logs, and run `faultline stop` if a fault may still be active.")


async def remeasure(ctx, since: float, timeout_s: float, halted) -> float | None:
    end = time.time() + timeout_s
    while time.time() < end and not halted():
        await asyncio.sleep(ctx.cfg.watchdog.interval_s)
        now = time.time()
        if watchdog.recovered(ctx.store.query(since=since), ctx.thresholds, now, since, ctx.cfg):
            return now
    return None


async def _sleep_unless(halted, seconds: float):
    end = time.time() + seconds
    while time.time() < end and not halted():
        await asyncio.sleep(min(0.25, max(0.0, end - time.time())))


def settled(store, exp_id: str, recovery_s: float | None, measured: bool) -> dict:
    """Recovery needed no agent: record it deterministically (no LLM call)."""
    if recovery_s is not None:
        note = f"recovered in {recovery_s:.1f} s"
    else:
        note = "recovery not measured (operator stop)" if not measured else "no recovery to judge"
    store.add_trace(AGENT, MODEL, f"{exp_id} recovery", json.dumps({"outcome": "not needed", "note": note}), 0.0, False)
    return {"outcome": "not needed", "initial": "recovered" if recovery_s is not None else "not measured", "actions": [], "final_recovery_s": recovery_s, "note": note}


async def remediate(ctx) -> dict:
    """Called only when recovery was measured and failed. Diagnoses and, within the guardrails, remediates. Returns the record."""
    ra = ctx.cfg.recovery_agent
    log = ctx.log
    rec = {"outcome": "escalated", "initial": "failed", "initial_window_s": ctx.cfg.recovery.cap_s, "actions": ctx.actions, "final_recovery_s": None, "note": None}

    def halted():
        return ctx.stop.is_set() or ctx.stop_path.exists()

    def end(outcome: str, note: str, message: bool = True):
        rec.update(outcome=outcome, note=note)
        if outcome == "escalated" and message:
            log(operator_message(note))
        return rec

    if not ra.enabled:
        return end("escalated", "recovery agent disabled ([recovery_agent] enabled = false)")
    for step in range(1, ra.max_actions + 1):
        if halted():
            return end("escalated" if ctx.actions else "not needed", "operator stop or kill switch; agent does nothing", bool(ctx.actions))
        now = time.time()
        deg = degraded_journeys(ctx, now)
        info = await asyncio.to_thread(gather, ctx, deg)
        out, meta = await asyncio.to_thread(diagnose, ctx, build_input(ctx, deg, info, now))
        if out is None:
            record(ctx, step, "escalate", None, "", "", f"no diagnosis: {meta['error']}")
            return end("escalated", f"recovery agent could not diagnose: {meta['error']}")
        ok, why = check_action(ctx, out, [d["journey"] for d in deg])
        if not ok:
            record(ctx, step, out.action, out.service, out.reason, out.diagnosis, f"rejected by guardrails: {why}")
            return end("escalated", f"proposed {out.action} {out.service or ''} rejected: {why}".replace("  ", " "))
        if out.action == "escalate":
            record(ctx, step, "escalate", None, out.reason, out.diagnosis, "no action")
            log(f"Recovery agent diagnosis: {out.diagnosis}")
            return end("escalated", out.reason)
        if halted():
            return end("escalated", "operator stop or kill switch before the action; agent does nothing")
        log(f"Recovery agent diagnosis: {out.diagnosis}")
        log(f"Recovery agent action {step}/{ra.max_actions}: {out.action}{' ' + out.service if out.service else ''} ({out.reason})")
        result = await asyncio.to_thread(execute, ctx, out)
        record(ctx, step, out.action, out.service, out.reason, out.diagnosis, result)
        log(f"Recovery agent result: {result}")
        if out.action == "kill_switch":
            return end("escalated", "kill switch used; run stopped, operator should inspect the system")
        await _sleep_unless(halted, ra.settle_s)
        got = await remeasure(ctx, time.time(), ra.remeasure_s, halted)
        if got is not None:
            rec.update(outcome="remediated", final_recovery_s=got - ctx.t_revert, note=f"back in baseline band after {out.action}")
            log(f"Recovery agent: remediated, recovered {got - ctx.t_revert:.1f} s after the revert")
            return rec
        log("Recovery agent: still not back in the baseline band")
    return end("escalated", f"still not back in the baseline band after {len(ctx.actions)} action(s)")


def describe(rec: dict | None) -> str:
    if not rec:
        return "Recovery agent: no record"
    acts = "; ".join(f"{a['action']}{' ' + a['service'] if a['service'] else ''} ({a['result']})" for a in rec["actions"] if a["action"] != "escalate" or a["result"] != "no action")
    if rec["outcome"] == "not needed":
        return "Recovery agent: " + ("recovered, no action" if rec["initial"] == "recovered" else f"no action ({rec.get('note')})")
    if rec["outcome"] == "remediated":
        return f"Recovery agent: remediated, did not self-recover in {rec['initial_window_s']} s; {acts}; recovered {rec['final_recovery_s']:.1f} s after the revert"
    return f"Recovery agent: escalated to the operator ({rec.get('note')})" + (f"; actions: {acts}" if acts else "")
