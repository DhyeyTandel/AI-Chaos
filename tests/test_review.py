import json

import pytest

from faultline import baseline as bl
from faultline import graph as plan_graph
from faultline import runs
from faultline.agents import safety
from test_memory import add_abort
from test_planner import FakeChat, FakeMsg, fresh_limiter, gmap  # noqa: F401
from test_run import Control, Script, go, rcfg  # noqa: F401


@pytest.fixture(autouse=True)
def no_cache(monkeypatch):
    monkeypatch.setenv("FAULTLINE_LLM_CACHE", "0")


def seed(store):
    th = {"home": {"error_rate_max": 0.2, "p95_max_ms": 800}}
    base = {"home": {"error_rate": 0.0, "p95_ms": 50.0, "count": 60, "success_rate": 1.0}}
    bl.save(store, {"journeys": base, "thresholds": th}, {})
    return bl.latest(store)


def reply(decision="approve", reason="fine", reduced=None):
    return json.dumps({"decision": decision, "reason": reason, **({"reduced_params": reduced} if reduced else {})})


DELAY = {"delay_ms": 300.0, "jitter_ms": 0.0, "loss_pct": 0.0}


def review(cfg, store, content, fault="delay", params=None, duration=20):
    base = seed(store)
    return safety.preflight_review(
        cfg, store, base, fault, "quote", duration, DELAY if params is None else params, 2, None, gmap(), FakeChat(lambda n: content)
    )


def test_reduce_only_lowers(cfg, store):
    r = review(cfg, store, reply("reduce", "too close to limit", {"delay_ms": 100}))
    assert r["decision"] == "reduce" and r["final"]["params"]["delay_ms"] == 100 and r["original"]["params"]["delay_ms"] == 300
    assert safety.describe_review(r) == "reduced delay 300 -> 100 ms: too close to limit"
    r = review(cfg, store, reply("reduce", "shorter", {"duration_s": 10}))
    assert r["decision"] == "reduce" and r["final"]["duration_s"] == 10 and r["final"]["params"]["delay_ms"] == 300


@pytest.mark.parametrize("reduced", [
    {"delay_ms": 400}, {"duration_s": 40}, {"loss_pct": 5}, {"delay_ms": 0}, {"duration_s": 5}, {"delay_ms": 300}, {"jitter_ms": 0}, {"delay_ms": "x"},
])
def test_raising_or_invalid_reduction_is_veto(cfg, store, reduced):
    r = review(cfg, store, reply("reduce", "try", reduced))
    assert r["decision"] == "veto" and r["final"] is None and r["fallback"] == 0
    assert "reduction rejected" in r["reason"]


def test_crash_only_shorter_duration(cfg, store):
    ok = review(cfg, store, reply("reduce", "short", {"duration_s": 10}), fault="crash", params={})
    assert ok["decision"] == "reduce" and ok["final"]["duration_s"] == 10
    bad = review(cfg, store, reply("reduce", "weaker", {"delay_ms": 10}), fault="crash", params={})
    assert bad["decision"] == "veto"
    below = review(cfg, store, reply("reduce", "tiny", {"duration_s": 9}), fault="crash", params={})
    assert below["decision"] == "veto"


def test_loss_cannot_reach_zero_and_delay_cannot_gain_loss(cfg, store):
    lp = {"delay_ms": 1.0, "jitter_ms": 0.0, "loss_pct": 10.0}
    assert review(cfg, store, reply("reduce", "less", {"loss_pct": 5}), params=lp)["final"]["params"]["loss_pct"] == 5
    assert review(cfg, store, reply("reduce", "none", {"loss_pct": 0}), params=lp)["decision"] == "veto"


def test_failure_approves_with_fallback(cfg, store):
    r = review(cfg, store, "not json")
    assert r["decision"] == "approve" and r["fallback"] == 1 and r["final"] == r["original"]
    assert "approved (fallback" in safety.describe_review(r)
    tr = [t for t in store.traces(10) if t["agent"] == "safety"]
    assert tr and tr[-1]["fallback"] == 1 and tr[-1]["input_summary"].startswith("preflight")


def test_no_key_approves_with_fallback(cfg, store, monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    base = seed(store)
    r = safety.preflight_review(cfg, store, base, "delay", "quote", 20, DELAY, 2, None, gmap(), None)
    assert r["decision"] == "approve" and r["fallback"] == 1 and "GROQ_API_KEY" in r["error"]


def test_disabled_review_returns_none(cfg, store):
    cfg.safety_agent.review = False
    assert review(cfg, store, reply()) is None


def test_review_input_has_limits_and_neighbour_outcomes(cfg, store):
    add_abort(store, "exp-a", "quote", params={"delay_ms": 100, "jitter_ms": 0, "loss_pct": 0})
    add_abort(store, "exp-b", "frontend", fault="crash", params={"duration_s": 20}, source="safety_agent")
    seen = []

    class Spy(FakeChat):
        def invoke(self, msgs):
            seen.append(msgs[1][1])
            return super().invoke(msgs)

    base = seed(store)
    safety.preflight_review(cfg, store, base, "delay", "quote", 20, DELAY, 2, {"hypothesis": "hyp"}, gmap(), Spy(lambda n: reply()))
    text = seen[0]
    assert "delay 300ms (delay) on quote, duration 20s, tier 2" in text and "Hypothesis: hyp" in text
    assert "quote delay >= 100ms (exp-a)" in text and "frontend (neighbour) crash -> ABORTED by safety_agent" in text
    assert "home: baseline err 0% p95 50ms; abort limits err 20% p95 800ms" in text


class Router(FakeChat):
    def __init__(self, review_fn):
        super().__init__(None)
        self.review_fn, self.reviews = review_fn, 0

    def invoke(self, msgs):
        self.calls += 1
        if "pre-flight" in msgs[0][1]:
            self.reviews += 1
            return FakeMsg(self.review_fn(self.reviews, msgs[1][1]))
        return FakeMsg("not json")


def graph_run(rcfg, store, chat, rounds=1, script=None):
    seed(store)
    script = script or Script()
    return go(rcfg, store, rounds, script, chat=chat), script


def test_veto_moves_to_next_candidate_without_abort(rcfg, store):
    chat = Router(lambda n, u: reply("veto", "too risky") if n == 1 else reply())
    run, script = graph_run(rcfg, store, chat)
    row = runs.rounds(store, run["run_id"])[0]
    pf = row["preflight"]
    assert pf["vetoed"] and pf["vetoed"][0]["reason"] == "too risky" and pf["decision"] == "approve"
    assert script.calls[0].id != pf["vetoed"][0]["candidate"] and row["candidate_id"] == script.calls[0].id
    assert row["outcome"] == "COMPLETED" and "aborted" not in row["climb_decision"] and chat.reviews == 2


def test_veto_does_not_exclude_target_permanently(rcfg, store):
    chat = Router(lambda n, u: reply("veto", "no") if n == 1 else reply())
    seed(store)
    script = Script()
    st = plan_graph.build_run_graph(rcfg, gmap(), store, script, lambda: ("ok", ""), Control(), chat=chat, log=lambda m: None)
    run_id = runs.new_run_id()
    runs.start(store, run_id, 1)
    init = {"run_id": run_id, "rounds": 1, "round": 1, "tier": 1, "survived_at_tier": 0, "survived_total": 0, "excluded": [],
            "vetoed": [], "review": None, "final": None, "memory_limits": [], "memory_excluded": [], "results": [],
            "experiment": None, "stop_reason": None}
    state = st.invoke(init, config={"recursion_limit": 100})
    assert state["excluded"] == [script.calls[0].target] and chat.reviews == 2


def test_all_vetoed_halts_run(rcfg, store):
    chat = Router(lambda n, u: reply("veto", "no"))
    run, script = graph_run(rcfg, store, chat)
    assert not script.calls and run["rounds_done"] == 0 and "vetoed" in run["stop_reason"]
    assert chat.reviews == rcfg.planner.max_attempts


def test_reduction_is_what_runs_and_is_recorded(rcfg, store):
    chat = Router(lambda n, u: reply("reduce", "shorter run", {"duration_s": 10}))
    run, script = graph_run(rcfg, store, chat)
    assert script.calls[0].duration_s == 10 and script.reviews[0]["decision"] == "reduce"
    pf = runs.rounds(store, run["run_id"])[0]["preflight"]
    assert pf["original"]["duration_s"] == 20 and pf["final"]["duration_s"] == 10 and pf["fallback"] == 0


def test_review_failure_still_runs_with_fallback(rcfg, store):
    run, script = graph_run(rcfg, store, FakeChat(lambda n: "garbage"))
    pf = runs.rounds(store, run["run_id"])[0]["preflight"]
    assert script.calls and pf["decision"] == "approve" and pf["fallback"] == 1


def test_review_disabled_skips_step(rcfg, store):
    rcfg.safety_agent.review = False
    chat = Router(lambda n, u: reply("veto", "no"))
    run, script = graph_run(rcfg, store, chat)
    assert script.calls and chat.reviews == 0 and runs.rounds(store, run["run_id"])[0]["preflight"] is None
