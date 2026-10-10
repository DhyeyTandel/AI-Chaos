from typing import TypedDict

from langgraph.graph import END, StateGraph

from .agents import planner, validator


class PlanState(TypedDict, total=False):
    top_k: int
    plan: planner.PlanResult
    cursor: int
    selected: planner.Scored | None
    validation: tuple[bool, str] | None
    rejected: list[dict]


def build_graph(cfg, graph_map: dict, store, chat=None):
    def plan_node(state: PlanState) -> PlanState:
        res = planner.plan(cfg, graph_map, store, state.get("top_k"), chat=chat)
        return {"plan": res, "cursor": 0, "rejected": [], "selected": None, "validation": None}

    def validate_node(state: PlanState) -> PlanState:
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

    def after_validate(state: PlanState) -> str:
        ok, _ = state["validation"]
        if ok or state["cursor"] >= cfg.planner.max_attempts or state["cursor"] >= len(state["plan"].ranked):
            return "end"
        return "validate"

    g = StateGraph(PlanState)
    g.add_node("plan", plan_node)
    g.add_node("validate", validate_node)
    g.set_entry_point("plan")
    g.add_edge("plan", "validate")
    g.add_conditional_edges("validate", after_validate, {"validate": "validate", "end": END})
    return g.compile()


def run_plan(cfg, graph_map: dict, store, top_k: int | None = None, chat=None) -> PlanState:
    return build_graph(cfg, graph_map, store, chat).invoke({"top_k": top_k or cfg.planner.top_k})
