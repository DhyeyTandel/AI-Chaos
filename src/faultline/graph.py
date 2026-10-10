import time
from typing import TypedDict

from langgraph.graph import END, StateGraph

from . import analyzer, faults, memory, report, runs
from . import baseline as bl
from .agents import analyst, fixer, planner, safety, validator


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
    vetoed: list[dict]
    review: dict | None
    final: planner.Candidate | None
    memory_limits: list[dict]
    memory_excluded: list[dict]
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


TIER_LEGEND = "tier 1 light delay or crash of an off-journey service; 2 delay 300ms, loss, traffic spike, crash with partial journey impact; 3 crash of a journey-critical or stateful service"


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
        seen = {x["candidate"] for x in state["memory_excluded"]}
        fresh = [x for x in res.exclusions if x["candidate"] not in seen]
        for x in fresh:
            log(f"memory: excluded {x['candidate']}: {x['reason']}")
        memory_excluded = [*state["memory_excluded"], *fresh]
        if fresh:
            runs.update(store, state["run_id"], memory={"limits": state["memory_limits"], "exclusions": memory_excluded})
        mem = {"memory_excluded": memory_excluded}
        if not res.ranked:
            return {"stop_reason": f"no eligible candidates at tier {state['tier']}", **mem}
        return {"plan": res, "cursor": 0, "rejected": [], "vetoed": [], "selected": None, "validation": None, **mem}

    def validate_node(state: RunState) -> RunState:
        return _validate_step(cfg, store, state)

    def after_validate(state: RunState) -> str:
        if state["validation"][0]:
            return "review"
        return "validate" if not _validate_done(cfg, state) else "halt"

    def halt_node(state: RunState) -> RunState:
        if state["vetoed"]:
            return {"stop_reason": f"every candidate was rejected or vetoed by the safety review ({state['validation'][1]})"}
        return {"stop_reason": f"validator rejected every candidate ({state['validation'][1]})"}

    def review_node(state: RunState) -> RunState:
        pick = state["selected"]
        c = pick.candidate
        why = control.reason()
        if why:
            return {"stop_reason": why, "review": None, "final": None}
        hyp, blast = planner.hypothesis_for(state["plan"], graph_map, pick)
        rv = safety.preflight_review(
            cfg, store, bl.latest(store), c.fault, c.target, c.duration_s, c.params, c.tier,
            safety.context_from_map(graph_map, c.target, hyp, blast), graph_map, chat,
        )
        if rv is None:
            return {"review": None, "final": c}
        log(f"round {state['round']}/{state['rounds']}: Safety review: {safety.describe_review(rv)}")
        if rv["decision"] == "veto":
            vetoed = [*state["vetoed"], {"candidate": c.id, "reason": rv["reason"]}]
            return {"selected": None, "validation": (False, f"{c.id} vetoed: {rv['reason']}"), "vetoed": vetoed, "review": rv, "final": None}
        if rv["decision"] == "reduce":
            final = c.model_copy(update={"params": rv["final"]["params"], "duration_s": int(rv["final"]["duration_s"])})
            return {"review": rv, "final": final}
        return {"review": rv, "final": c}

    def after_review(state: RunState) -> str:
        if state.get("stop_reason"):
            return "end"
        if state["selected"] is not None:
            return "execute"
        return "validate" if not _validate_done(cfg, state) else "halt"

    def execute_node(state: RunState) -> RunState:
        why = control.reason()
        if why:
            return {"stop_reason": why, "experiment": None}
        c = state["final"] or state["selected"].candidate
        hyp, blast = planner.hypothesis_for(state["plan"], graph_map, state["selected"])
        log(f"round {state['round']}/{state['rounds']}: running {c.id}")
        try:
            exp = runner(c, safety.context_from_map(graph_map, c.target, hyp, blast), state["review"])
        except faults.ValidationError as e:
            return {"stop_reason": f"experiment rejected: {e}", "experiment": None}
        if exp is None:
            return {"stop_reason": control.reason() or "interrupted", "experiment": None}
        return {"experiment": exp}

    def record_node(state: RunState) -> RunState:
        exp, pick, res = state["experiment"], state["selected"], state["plan"]
        c, rnd = state["final"] or pick.candidate, state["round"]
        hyp, blast = planner.hypothesis_for(res, graph_map, pick)
        survived = exp["state"] == "COMPLETED"
        op_stop = runs.is_operator_stop(exp["abort_reason"])
        tier, at_tier, decision = climb_decision(cfg, state["tier"], state["survived_at_tier"], survived, op_stop, c.target, exp["abort_reason"])
        row = {
            "round": rnd, "tier": state["tier"], "candidate_id": pick.candidate.id, "fault": c.fault, "target": c.target, "params": c.params,
            "llm_rank": pick.llm_rank, "reason": pick.reason, "fallback": int(res.fallback), "hypothesis": hyp,
            "predicted_blast_radius": blast, "experiment_id": exp["id"], "outcome": exp["state"],
            "abort_reason": exp["abort_reason"], "recovery_s": exp["recovery_seconds"], "climb_decision": decision,
            "preflight": {**state["review"], "vetoed": state["vetoed"]} if state["review"] or state["vetoed"] else None,
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
    for name, fn in (("plan", plan_node), ("validate", validate_node), ("review", review_node), ("halt", halt_node), ("execute", execute_node),
                     ("record", record_node), ("settle", settle_node)):
        g.add_node(name, fn)
    g.set_entry_point("plan")
    g.add_conditional_edges("plan", end_if_stopped("validate"), {"validate": "validate", "end": END})
    g.add_conditional_edges("validate", after_validate, {"validate": "validate", "review": "review", "halt": "halt"})
    g.add_conditional_edges("review", after_review, {"validate": "validate", "execute": "execute", "halt": "halt", "end": END})
    g.add_edge("halt", END)
    g.add_conditional_edges("execute", end_if_stopped("record"), {"record": "record", "end": END})
    g.add_conditional_edges("record", end_if_stopped("settle"), {"settle": "settle", "end": END})
    g.add_conditional_edges("settle", end_if_stopped("plan"), {"plan": "plan", "end": END})
    return g.compile()


def run_loop(cfg, graph_map: dict, store, rounds: int, runner, settler, control, chat=None, log=print, on_round=None) -> dict:
    run_id = runs.new_run_id()
    runs.start(store, run_id, rounds)
    limits = memory.known_limits(cfg, store)
    runs.update(store, run_id, memory={"limits": limits, "exclusions": []})
    init = {
        "run_id": run_id, "rounds": rounds, "round": 1, "tier": 1, "survived_at_tier": 0, "survived_total": 0,
        "excluded": [], "vetoed": [], "review": None, "final": None, "memory_limits": limits, "memory_excluded": [],
        "results": [], "experiment": None, "stop_reason": None,
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


class ReportState(TypedDict, total=False):
    run: dict
    rounds: list[dict]
    since: float
    analysis: dict
    analyst: analyst.AnalystResult
    fixer: fixer.FixerResult
    data: dict
    paths: dict


def build_report_graph(cfg, graph_map: dict, store, out_root, chat=None):
    def analyze_node(state: ReportState) -> ReportState:
        run, rounds = state["run"], state["rounds"]
        analysis = analyzer.analyze(cfg, store, graph_map, run, rounds)
        known = {n for n, a in graph_map["nodes"].items() if a.get("kind") in ("app", "infra")}
        return {"analysis": analysis, "analyst": analyst.analyze(cfg, store, analysis, known, chat=chat)}

    def fix_node(state: ReportState) -> ReportState:
        return {"fixer": fixer.fix(cfg, store, state["analysis"], graph_map, state["analyst"].weaknesses, chat=chat)}

    def report_node(state: ReportState) -> ReportState:
        data = report.build(cfg, store, state["run"], state["rounds"], state["analysis"], state["analyst"], state["fixer"], state["since"])
        return {"data": data, "paths": report.write(cfg, data, out_root)}

    g = StateGraph(ReportState)
    g.add_node("analyze", analyze_node)
    g.add_node("fix", fix_node)
    g.add_node("report", report_node)
    g.set_entry_point("analyze")
    g.add_edge("analyze", "fix")
    g.add_edge("fix", "report")
    g.add_edge("report", END)
    return g.compile()


def run_report(cfg, graph_map: dict, store, out_root, run_id: str | None = None, chat=None) -> ReportState | None:
    run = runs.get(store, run_id)
    if run is None:
        return None
    init = {"run": run, "rounds": runs.rounds(store, run["run_id"]), "since": time.time() - 1}
    return build_report_graph(cfg, graph_map, store, out_root, chat).invoke(init)
