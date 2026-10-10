import json

import pytest

from faultline import graph as plan_graph
from faultline import llm
from faultline.agents import planner, validator
from faultline.ledger import Ledger


def gmap():
    nodes = {
        "frontend": {"stateful": False, "kind": "app"},
        "cart": {"stateful": False, "kind": "app"},
        "checkout": {"stateful": False, "kind": "app"},
        "valkey-cart": {"stateful": True, "kind": "infra"},
        "quote": {"stateful": False, "kind": "app"},
        "grafana": {"stateful": False, "kind": "tooling"},
    }
    edges = [
        {"source": "frontend", "target": "cart"}, {"source": "checkout", "target": "cart"},
        {"source": "cart", "target": "valkey-cart"}, {"source": "frontend", "target": "quote"},
    ]
    journeys = {
        "add_to_cart": {"services": ["frontend", "cart", "valkey-cart"]},
        "home": {"services": ["frontend"]},
    }
    return {"nodes": nodes, "edges": edges, "journeys": journeys}


class FakeMsg:
    def __init__(self, content):
        self.content = content


class FakeChat:
    def __init__(self, content_fn):
        self.content_fn = content_fn
        self.calls = 0

    def bind(self, **kw):
        return self

    def invoke(self, msgs):
        self.calls += 1
        return FakeMsg(self.content_fn(self.calls))


@pytest.fixture(autouse=True)
def fresh_limiter():
    llm.reset_limiter(llm.TokenBucket(1000))
    yield
    llm.reset_limiter(None)


def top_ids(cfg, store):
    cands = planner.generate_candidates(cfg, gmap())
    scored = planner.score_candidates(cfg, gmap(), [], cands)
    return [s.candidate.id for s in planner.select_top(cfg, scored, 8)]


def test_candidates_respect_allowlist_and_tooling(cfg):
    ids = [c.id for c in planner.generate_candidates(cfg, gmap())]
    assert not any("grafana" in i for i in ids)
    assert "crash:cart:20s" in ids and "delay:quote:100ms" in ids and "loss:quote:10pct" in ids


def test_scoring_is_deterministic_and_ordered(cfg, store):
    assert top_ids(cfg, store) == top_ids(cfg, store)
    assert top_ids(cfg, store)[0].split(":")[1] in ("cart", "valkey-cart")


def test_valid_llm_ranking_used(cfg, store):
    ids = top_ids(cfg, store)
    order = list(reversed(ids))
    out = {"ranking": [{"candidate_id": c, "rank": i + 1, "reason": "r" * 300} for i, c in enumerate(order)],
           "hypothesis": "h", "predicted_blast_radius": ["frontend"]}
    res = planner.plan(cfg, gmap(), store, chat=FakeChat(lambda n: json.dumps(out)))
    assert not res.fallback
    assert [s.candidate.id for s in res.ranked] == order
    assert res.ranked[0].llm_rank == 1 and len(res.ranked[0].reason) == 200
    assert res.hypothesis == "h" and res.blast_radius == ["frontend"]
    assert store.traces()[-1]["agent"] == "planner" and store.traces()[-1]["fallback"] == 0


def test_invented_candidate_ignored_and_missing_appended(cfg, store):
    ids = top_ids(cfg, store)
    out = {"ranking": [{"candidate_id": "crash:evil:5s", "rank": 1, "reason": "x"},
                       {"candidate_id": ids[3], "rank": 2, "reason": "y"}],
           "hypothesis": "h", "predicted_blast_radius": []}
    res = planner.plan(cfg, gmap(), store, chat=FakeChat(lambda n: json.dumps(out)))
    got = [s.candidate.id for s in res.ranked]
    assert "crash:evil:5s" not in got
    assert got[0] == ids[3]
    assert got[1:] == [i for i in ids if i != ids[3]]
    assert sorted(got) == sorted(ids)


def test_malformed_output_falls_back(cfg, store):
    chat = FakeChat(lambda n: "not json at all")
    res = planner.plan(cfg, gmap(), store, chat=chat)
    assert res.fallback and "invalid output" in res.fallback_reason
    assert chat.calls == 2
    assert [s.candidate.id for s in res.ranked] == top_ids(cfg, store)
    assert res.hypothesis and all(s.reason for s in res.ranked)
    assert store.traces()[-1]["fallback"] == 1


def test_retry_recovers_after_one_bad_reply(cfg, store):
    ids = top_ids(cfg, store)
    good = json.dumps({"ranking": [{"candidate_id": ids[0], "rank": 1, "reason": "ok"}], "hypothesis": "h", "predicted_blast_radius": []})
    res = planner.plan(cfg, gmap(), store, chat=FakeChat(lambda n: "oops" if n == 1 else good))
    assert not res.fallback


def test_no_key_falls_back(cfg, store, monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    assert llm.get_chat("m") is None
    res = planner.plan(cfg, gmap(), store)
    assert res.fallback and "GROQ_API_KEY" in res.fallback_reason
    assert store.traces()[-1]["error"] == "no GROQ_API_KEY"


def test_validator_rejects_disallowed_target(cfg, store):
    c = planner.Candidate(id="crash:reaper:20s", fault="crash", kind="crash", target="reaper", params={}, duration_s=20, label="x")
    ok, reason = validator.validate_candidate(cfg, store, c)
    assert not ok and "allowlist" in reason
    row = store.traces()[-1]
    assert row["agent"] == "validator" and "reaper" in row["error"]


def test_validator_rejects_over_cap_and_crash_excluded(cfg, store):
    c = planner.Candidate(id="a", fault="crash", kind="crash", target="cart", params={}, duration_s=cfg.safety.max_duration_s + 1, label="x")
    assert not validator.validate_candidate(cfg, store, c)[0]
    c = planner.Candidate(id="b", fault="crash", kind="crash", target="frontend-proxy", params={}, duration_s=10, label="x")
    assert not validator.validate_candidate(cfg, store, c)[0]
    c = planner.Candidate(id="c", fault="delay", kind="d", target="cart", params={"delay_ms": 99999}, duration_s=10, label="x")
    assert not validator.validate_candidate(cfg, store, c)[0]


def test_validator_rejects_active_ledger_fault(cfg, store):
    Ledger(store).create("e", "crash", "cart", {}, {}, 9e9)
    c = planner.Candidate(id="a", fault="delay", kind="d", target="quote", params={"delay_ms": 100}, duration_s=10, label="x")
    ok, reason = validator.validate_candidate(cfg, store, c)
    assert not ok and "ledger" in reason


def test_graph_tries_next_candidate_after_rejection(cfg, store, monkeypatch):
    ids = top_ids(cfg, store)
    real = validator.validate_candidate
    monkeypatch.setattr(validator, "validate_candidate", lambda c, s, cand: (False, "nope") if cand.id == ids[0] else real(c, s, cand))
    state = plan_graph.run_plan(cfg, gmap(), store)
    assert state["rejected"] == [{"candidate": ids[0], "reason": "nope"}]
    assert state["selected"].candidate.id == ids[1]


def test_graph_gives_up_after_three_rejections(cfg, store, monkeypatch):
    monkeypatch.setattr(validator, "validate_candidate", lambda c, s, cand: (False, "nope"))
    state = plan_graph.run_plan(cfg, gmap(), store)
    assert state["selected"] is None and len(state["rejected"]) == 3


def test_graph_ends_with_no_plan_after_max_attempts(cfg, store):
    cfg.safety.allow = []
    state = plan_graph.run_plan(cfg, gmap(), store)
    assert state["selected"] is None and state["validation"][1] == "no candidates"


def test_rate_limiter_blocks_when_empty():
    t = [0.0]
    b = llm.TokenBucket(60, capacity=2, clock=lambda: t[0], sleep=lambda s: t.__setitem__(0, t[0] + s))
    assert b.try_acquire() and b.try_acquire()
    assert not b.try_acquire()
    assert not b.acquire(0.3)
    assert b.acquire(2.0)


def test_empty_bucket_makes_call_fail_without_raising(cfg, store):
    llm.reset_limiter(llm.TokenBucket(60, capacity=1))
    llm.limiter().try_acquire()
    chat = FakeChat(lambda n: "{}")
    out, meta = llm.call_structured("planner", "m", planner.PlannerOut, "s", "u", 0.05, store=store, chat=chat)
    assert out is None and "rate limiter" in meta["error"] and chat.calls == 0


class Err429(Exception):
    status_code = 429

    def __init__(self, ra):
        class R:
            headers = {"retry-after": str(ra)}
        self.response = R()


def test_429_long_retry_after_gives_up(cfg, store):
    class C(FakeChat):
        def invoke(self, msgs):
            self.calls += 1
            raise Err429(60)

    c = C(None)
    out, meta = llm.call_structured("planner", "m", planner.PlannerOut, "s", "u", 1, store=store, chat=c)
    assert out is None and c.calls == 1 and "429" in meta["error"]
