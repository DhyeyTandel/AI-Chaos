import json
import time

from faultline import memory
from faultline.agents import planner
from test_planner import FakeChat, fresh_limiter, gmap  # noqa: F401


def add_abort(store, eid, target, fault="delay", params=None, source="watchdog", state="ABORTED", age_days=0.0):
    t = time.time() - age_days * 86400
    with store.conn:
        store.conn.execute(
            "INSERT INTO experiments (id, fault, target, params, state, created_at, abort_source) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (eid, fault, target, json.dumps(params or {"delay_ms": 100, "jitter_ms": 0, "loss_pct": 0}), state, t, source),
        )


def cand_ids(cfg, store, **kw):
    cfg.planner.max_per_target = 99
    res = planner.plan(cfg, gmap(), store, 100, chat=FakeChat(lambda n: "bad"), **kw)
    return [s.candidate.id for s in res.ranked], res


def test_exclusion_at_and_above_known_intensity(cfg, store):
    add_abort(store, "exp-a", "quote", params={"delay_ms": 100, "jitter_ms": 0, "loss_pct": 0})
    ids, res = cand_ids(cfg, store)
    assert "delay:quote:50ms" in ids and "delay:quote:300ms" not in ids
    ex = {x["candidate"]: x for x in res.exclusions}
    assert ex["delay:quote:300ms"]["reason"] == "delay on quote aborted at 100 ms in exp-a; 300 ms excluded"
    assert ex["delay:quote:300ms"]["evidence"] == "exp-a"
    assert "loss:quote:10pct" in ids and "crash:quote:20s" in ids


def test_equal_intensity_is_excluded_and_lowest_wins(cfg, store):
    add_abort(store, "exp-hi", "quote", params={"delay_ms": 300, "jitter_ms": 0, "loss_pct": 0})
    add_abort(store, "exp-lo", "quote", params={"delay_ms": 50, "jitter_ms": 0, "loss_pct": 0})
    ids, res = cand_ids(cfg, store)
    assert "delay:quote:50ms" not in ids and "delay:quote:300ms" not in ids
    assert {x["evidence"] for x in res.exclusions} == {"exp-lo"}


def test_crash_and_loss_limits(cfg, store):
    add_abort(store, "exp-c", "cart", fault="crash", params={"duration_s": 20})
    add_abort(store, "exp-l", "quote", params={"delay_ms": 1, "jitter_ms": 0, "loss_pct": 10})
    ids, res = cand_ids(cfg, store)
    assert "crash:cart:20s" not in ids and "delay:cart:50ms" in ids
    assert "loss:quote:10pct" not in ids and "delay:quote:300ms" in ids
    reasons = {x["candidate"]: x["reason"] for x in res.exclusions}
    assert reasons["loss:quote:10pct"] == "loss on quote aborted at 10 % in exp-l; 10 % excluded"
    assert reasons["crash:cart:20s"] == "crash on cart aborted in exp-c; crash excluded"


def test_non_safety_aborts_are_ignored(cfg, store):
    for i, src in enumerate(("operator", "preflight", "inject", "system", "revert", None)):
        add_abort(store, f"exp-{i}", "quote", source=src)
    add_abort(store, "exp-done", "quote", state="COMPLETED", source=None)
    assert memory.known_limits(cfg, store) == []
    ids, res = cand_ids(cfg, store)
    assert "delay:quote:300ms" in ids and not res.exclusions


def test_safety_agent_abort_counts_and_lookback_applies(cfg, store):
    add_abort(store, "exp-sa", "quote", source="safety_agent")
    add_abort(store, "exp-old", "cart", age_days=8)
    limits = memory.known_limits(cfg, store)
    assert [(x["target"], x["experiment_id"], x["source"]) for x in limits] == [("quote", "exp-sa", "safety_agent")]
    cfg.planner.memory.lookback_days = 30
    assert {x["target"] for x in memory.known_limits(cfg, store)} == {"quote", "cart"}


def test_disabled_memory_excludes_nothing(cfg, store):
    add_abort(store, "exp-a", "quote")
    cfg.planner.memory.enabled = False
    ids, res = cand_ids(cfg, store)
    assert "delay:quote:300ms" in ids and not res.exclusions and not res.limits


def test_prompt_contains_known_limits(cfg, store):
    add_abort(store, "exp-a", "quote")
    seen = []

    class Spy(FakeChat):
        def invoke(self, msgs):
            seen.append(msgs)
            return super().invoke(msgs)

    planner.plan(cfg, gmap(), store, chat=Spy(lambda n: "bad"))
    system, user = seen[0][0][1], seen[0][1][1]
    assert "Known limits" in user and "quote delay >= 100ms (exp-a)" in user
    assert "known limits" in system
    seen.clear()
    cfg.planner.memory.enabled = False
    planner.plan(cfg, gmap(), store, chat=Spy(lambda n: "bad"))
    assert "Known limits" not in seen[0][1][1]


def test_legacy_rows_without_abort_source_use_reason_text(cfg, store):
    add_abort(store, "exp-old", "quote", source=None)
    with store.conn:
        store.conn.execute("UPDATE experiments SET abort_reason = 'watchdog breach: home p95 900ms > 800ms' WHERE id = 'exp-old'")
    add_abort(store, "exp-stop", "cart", source=None)
    with store.conn:
        store.conn.execute("UPDATE experiments SET abort_reason = 'kill switch (STOP file)' WHERE id = 'exp-stop'")
    assert [(x["target"], x["source"]) for x in memory.known_limits(cfg, store)] == [("quote", "watchdog")]
