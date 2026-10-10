import time
from typing import TypedDict

from langgraph.graph import END, StateGraph

from . import faults, runs
from .agents import planner, validator


class PlanState(TypedDict, total=False):
    top_k: int
    plan: planner.PlanResult
    cursor: int
    selected: planner.Scored | None
    validation: tuple[bool, str] | None
    rejected: list[dict]


class RunState(PlanState, total=False):
    run_id: str
    rounds: int
    round: int
    tier: int
    survived_at_tier: int
    survived_total: int
    excluded: list[str]
    results: list[dict]
    experiment: dict | None
    stop_reason: str | None


def _validate_step(cfg, store, state):
    ranked = state["plan"].ranked
    i = state["cursor"]
    if i >= len(ranked):
        return {"selected": None, "validation": (False, "no candidates"), "cursor": i}
    pick = ranked[i]
    ok, reason = validator.validate_candidate(cfg, store, pick.candidate)
    rejected = list(state["rejected"])
    if not ok:
        rejected.append({"candidate": pick.candidate.id, "reason": reason})
    return {"selected": pick if ok else None, "validation": (ok, reason), "cursor": i + 1, "rejected": rejected}


def _validate_done(cfg, state) -> bool:
    ok, _ = state["validation"]
    return ok or state["cursor"] >= cfg.planner.max_attempts or state["cursor"] >= len(state["plan"].ranked)


def build_graph(cfg, graph_map: dict, store, chat=None):
    def plan_node(state: PlanState) -> PlanState:
        res = planner.plan(cfg, graph_map, store, state.get("top_k"), chat=chat)
        return {"plan": res, "cursor": 0, "rejected": [], "selected": None, "validation": None}

    def validate_node(state: PlanState) -> PlanState:
        return _validate_step(cfg, store, state)

    def after_validate(state: PlanState) -> str:
        return "end" if _validate_done(cfg, state) else "validate"

    g = StateGraph(PlanState)
    g.add_node("plan", plan_node)
    g.add_node("validate", validate_node)
    g.set_entry_point("plan")
    g.add_edge("plan", "validate")
    g.add_conditional_edges("validate", after_validate, {"validate": "validate", "end": END})
    return g.compile()


def run_plan(cfg, graph_map: dict, store, top_k: int | None = None, chat=None) -> PlanState:
    return build_graph(cfg, graph_map, store, chat).invoke({"top_k": top_k or cfg.planner.top_k})


TIER_LEGEND = "tier 1 light delay or crash of an off-journey service; 2 delay 300ms, loss, crash with partial journey impact; 3 crash of a journey-critical or stateful service"


def strategy_text(cfg, state: RunState) -> str:
    done = "; ".join(f"{r['candidate_id']} {r['outcome'].lower()}" for r in state["results"]) or "none yet"
    return (
        f"survivable first, climb one tier after {cfg.run.climb_after} survived experiments ({TIER_LEGEND}). "
        f"Current tier {state['tier']}; round {state['round']} of {state['rounds']}. Results so far: {done}."
    )


def climb_decision(cfg, tier: int, at_tier: int, survived: bool, op_stop: bool, target: str, reason: str | None) -> tuple[int, int, str]:
    need = cfg.run.climb_after
    if op_stop:
        return tier, at_tier, "no climb: run stopped by operator"
    if not survived:
        return tier, at_tier, f"no climb: aborted ({(reason or 'unknown')[:70]}); {target} excluded"
    at_tier += 1
    if at_tier < need:
        return tier, at_tier, f"no climb: {at_tier}/{need} survived at tier {tier}"
    if tier >= 3:
        return tier, at_tier, "no climb: already at max tier 3"
    return tier + 1, 0, f"climb {tier}->{tier + 1}: {at_tier} survived at tier {tier}"


def build_run_graph(cfg, graph_map: dict, store, runner, settler, control, chat=None, log=print, on_round=None):
    def plan_node(state: RunState) -> RunState:
        why = control.reason()
        if why:
            return {"stop_reason": why}
        log(f"round {state['round']}/{state['rounds']} tier {state['tier']}: planning")
        res = planner.plan(
            cfg, graph_map, store, None, chat=chat, max_tier=state["tier"], exclude=set(state["excluded"]),
            strategy=strategy_text(cfg, state),
        )
        if not res.ranked:
            return {"stop_reason": f"no eligible candidates at tier {state['tier']}"}
        return {"plan": res, "cursor": 0, "rejected": [], "selected": None, "validation": None}

    def validate_node(state: RunState) -> RunState:
        return _validate_step(cfg, store, state)

    def after_validate(state: RunState) -> str:
        if state["validation"][0]:
            return "execute"
        return "validate" if not _validate_done(cfg, state) else "halt"

    def halt_node(state: RunState) -> RunState:
        return {"stop_reason": f"validator rejected every candidate ({state['validation'][1]})"}

    def execute_node(state: RunState) -> RunState:
        why = control.reason()
        if why:
            return {"stop_reason": why, "experiment": None}
        c = state["selected"].candidate
        log(f"round {state['round']}/{state['rounds']}: running {c.id}")
        try:
            exp = runner(c)
        except faults.ValidationError as e:
            return {"stop_reason": f"experiment rejected: {e}", "experiment": None}
        if exp is None:
            return {"stop_reason": control.reason() or "interrupted", "experiment": None}
        return {"experiment": exp}

    def record_node(state: RunState) -> RunState:
        exp, pick, res = state["experiment"], state["selected"], state["plan"]
        c, rnd = pick.candidate, state["round"]
        hyp, blast = planner.hypothesis_for(res, graph_map, pick)
        survived = exp["state"] == "COMPLETED"
        op_stop = runs.is_operator_stop(exp["abort_reason"])
        tier, at_tier, decision = climb_decision(cfg, state["tier"], state["survived_at_tier"], survived, op_stop, c.target, exp["abort_reason"])
        row = {
            "round": rnd, "tier": state["tier"], "candidate_id": c.id, "fault": c.fault, "target": c.target, "params": c.params,
            "llm_rank": pick.llm_rank, "reason": pick.reason, "fallback": int(res.fallback), "hypothesis": hyp,
            "predicted_blast_radius": blast, "experiment_id": exp["id"], "outcome": exp["state"],
            "abort_reason": exp["abort_reason"], "recovery_s": exp["recovery_seconds"], "climb_decision": decision,
        }
        runs.add_round(store, state["run_id"], row)
        runs.update(store, state["run_id"], rounds_done=rnd, final_tier=tier)
        if on_round:
            on_round({**row, "fallback_reason": res.fallback_reason, "target_tier": c.tier})
        excluded = list(state["excluded"])
        if c.target not in excluded:
            excluded.append(c.target)
        stop = (exp["abort_reason"] if op_stop else None) or control.reason()
        if stop is None and rnd >= state["rounds"]:
            stop = f"completed {rnd} rounds"
        return {
            "tier": tier, "survived_at_tier": at_tier, "survived_total": state["survived_total"] + survived,
            "excluded": excluded, "results": [*state["results"], {"candidate_id": c.id, "outcome": exp["state"]}],
            "round": rnd + 1, "experiment": None, "stop_reason": stop,
        }

    def settle_node(state: RunState) -> RunState:
        end = time.time() + cfg.run.cooldown_s
        while time.time() < end and not control.reason():
            time.sleep(min(0.5, max(0.0, end - time.time())))
        why = control.reason()
        if why:
            return {"stop_reason": why}
        log(f"cooldown done; waiting for baseline (up to {cfg.run.settle_wait_s}s)")
        status, detail = settler()
        if status == "stopped":
            return {"stop_reason": control.reason() or detail}
        if status != "ok":
            log(f"not back to baseline: {detail}")
            return {"stop_reason": "system did not return to baseline"}
        return {}

    def end_if_stopped(nxt: str):
        return lambda state: "end" if state.get("stop_reason") else nxt

    g = StateGraph(RunState)
    for name, fn in (("plan", plan_node), ("validate", validate_node), ("halt", halt_node), ("execute", execute_node),
                     ("record", record_node), ("settle", settle_node)):
        g.add_node(name, fn)
    g.set_entry_point("plan")
    g.add_conditional_edges("plan", end_if_stopped("validate"), {"validate": "validate", "end": END})
    g.add_conditional_edges("validate", after_validate, {"validate": "validate", "execute": "execute", "halt": "halt"})
    g.add_edge("halt", END)
    g.add_conditional_edges("execute", end_if_stopped("record"), {"record": "record", "end": END})
    g.add_conditional_edges("record", end_if_stopped("settle"), {"settle": "settle", "end": END})
    g.add_conditional_edges("settle", end_if_stopped("plan"), {"plan": "plan", "end": END})
    return g.compile()


def run_loop(cfg, graph_map: dict, store, rounds: int, runner, settler, control, chat=None, log=print, on_round=None) -> dict:
    run_id = runs.new_run_id()
    runs.start(store, run_id, rounds)
    init = {
        "run_id": run_id, "rounds": rounds, "round": 1, "tier": 1, "survived_at_tier": 0, "survived_total": 0,
        "excluded": [], "results": [], "experiment": None, "stop_reason": None,
    }
    try:
        state = build_run_graph(cfg, graph_map, store, runner, settler, control, chat, log, on_round).invoke(
            init, config={"recursion_limit": 400}
        )
    except BaseException as e:
        runs.update(store, run_id, ended_at=time.time(), stop_reason="interrupted" if isinstance(e, KeyboardInterrupt) else f"error: {type(e).__name__}: {e}"[:200])
        raise
    runs.update(store, run_id, ended_at=time.time(), stop_reason=state.get("stop_reason") or "ended")
    return runs.get(store, run_id)
