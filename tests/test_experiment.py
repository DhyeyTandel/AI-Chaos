import asyncio
import json
import time

import pytest

from faultline import baseline as bl
from faultline import experiment as exp_mod
from faultline import faults
from faultline.agents import safety
from faultline.injector import Injector
from faultline.store import Store
from faultline.ledger import APPLIED, REVERTED, Ledger
from conftest import FakeDriver
from test_planner import FakeChat, fresh_limiter  # noqa: F401

JOURNEYS = ("home", "cart")


class Sim:
    """Fake prober: journeys fail while a fault is applied and `bad` is set."""

    def __init__(self):
        self.faulted = False
        self.bad = False


class SimDriver(FakeDriver):
    def __init__(self, sim, on_apply=None, fail_apply=False):
        super().__init__()
        self.sim, self.on_apply, self.fail_apply = sim, on_apply, fail_apply

    def apply(self, entry):
        if self.on_apply:
            self.on_apply(entry)
        if self.fail_apply:
            raise RuntimeError("no docker")
        self.sim.faulted = True
        return super().apply(entry)

    def revert(self, entry):
        super().revert(entry)
        if entry["experiment_id"] != "other":
            self.sim.faulted = False


class SlowChat(FakeChat):
    def __init__(self, content_fn, delay):
        super().__init__(content_fn)
        self.delay = delay

    def invoke(self, msgs):
        time.sleep(self.delay)
        return super().invoke(msgs)


def verdict(decision, reason="home errors spreading", concern="high"):
    return json.dumps({"decision": decision, "reason": reason, "concern_level": concern})


@pytest.fixture
def ecfg(cfg, store, tmp_path, monkeypatch):
    monkeypatch.setenv("FAULTLINE_DB", store.path)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    cfg.preflight.probe_s = 0.6
    cfg.watchdog.interval_s = 0.1
    cfg.watchdog.window_s = 2
    cfg.watchdog.min_samples = 3
    cfg.recovery.window_s = 1
    cfg.recovery.min_samples = 3
    cfg.recovery.cap_s = 3
    cfg.reaper.heartbeat_s = 0.2
    cfg.safety_agent.first_check_s = 0.3
    cfg.safety_agent.interval_s = 0.5
    cfg.safety_agent.timeout_s = 2
    safety._warned = False
    th = {j: {"error_rate_max": 0.2, "p95_max_ms": 800} for j in JOURNEYS}
    base = {j: {"error_rate": 0.0, "p95_ms": 50.0, "count": 60, "success_rate": 1.0} for j in JOURNEYS}
    bl.save(store, {"journeys": base, "thresholds": th}, {})
    return cfg


def go(monkeypatch, ecfg, store, driver, sim, duration=2, chat=None, context=None, log=None, **kw):
    async def fake_probe(st, seconds, timeout=3.0, stop=None):
        while not stop.is_set():
            t = time.time()
            ok = not (sim.faulted and sim.bad)
            st.insert_many([(t, 0.0, j, ok, 200, 50.0, None) for j in JOURNEYS])
            await asyncio.sleep(0.05)

    monkeypatch.setattr(exp_mod, "run_probe", fake_probe)
    return asyncio.run(exp_mod.run_experiment(
        store, driver, ecfg, "crash", "ad", duration, {}, log=log or (lambda m: None), context=context, chat=chat, **kw,
    ))


def add_other(store, exp_id="other", target="quote"):
    own = Store(store.path)
    ledger = Ledger(own)
    i = ledger.create(exp_id, "delay", target, {"delay_ms": 100}, faults.initial_revert("delay", target), time.time() + 1000)
    ledger.mark_applied(i)
    own.close()
    return i


def test_revert_experiment_only_own_entries(store, driver, cfg):
    ledger = Ledger(store)
    inj = Injector(ledger, driver, cfg, retry_delay=0)
    mine = inj.inject("e1", "crash", "cart", {}, 10)
    other = add_other(store, "e2")
    assert inj.revert_experiment("e1") == {mine: REVERTED}
    assert ledger.get(mine)["state"] == REVERTED and ledger.get(other)["state"] == APPLIED
    assert inj.revert_experiment("e1") == {}


def test_revert_all_is_global(store, driver, cfg):
    ledger = Ledger(store)
    inj = Injector(ledger, driver, cfg, retry_delay=0)
    a = inj.inject("e1", "crash", "cart", {}, 10)
    b = add_other(store, "e2")
    assert inj.revert_all() == {a: REVERTED, b: REVERTED}


def test_inject_failure_leaves_other_experiment_applied(monkeypatch, ecfg, store):
    sim = Sim()
    other = []
    driver = SimDriver(sim, on_apply=lambda e: other.append(add_other(store)), fail_apply=True)
    e = go(monkeypatch, ecfg, store, driver, sim)
    assert e["state"] == "ABORTED" and e["abort_reason"].startswith("inject failed") and e["abort_source"] == "inject"
    ledger = Ledger(store)
    assert ledger.get(other[0])["state"] == APPLIED
    assert [x["state"] for x in ledger.all() if x["experiment_id"] == e["id"]] == [REVERTED]


def test_refused_while_other_fault_active_does_not_touch_it(monkeypatch, ecfg, store):
    sim = Sim()
    other = add_other(store)
    driver = SimDriver(sim)
    e = go(monkeypatch, ecfg, store, driver, sim)
    assert e["state"] == "ABORTED" and e["abort_reason"].startswith("preflight") and e["abort_source"] == "preflight"
    assert Ledger(store).get(other)["state"] == APPLIED and driver.reverts == 0


def test_watchdog_abort_reverts_only_own(monkeypatch, ecfg, store):
    sim = Sim()
    sim.bad = True
    other = []
    driver = SimDriver(sim, on_apply=lambda e: other.append(add_other(store)))
    e = go(monkeypatch, ecfg, store, driver, sim, duration=5)
    assert e["state"] == "ABORTED" and e["abort_source"] == "watchdog" and e["abort_reason"].startswith("watchdog")
    ledger = Ledger(store)
    assert ledger.get(other[0])["state"] == APPLIED
    assert [x["state"] for x in ledger.all() if x["experiment_id"] == e["id"]] == [REVERTED]


def test_safety_abort_reverts_only_own(monkeypatch, ecfg, store):
    sim = Sim()
    other = []
    driver = SimDriver(sim, on_apply=lambda e: other.append(add_other(store)))
    chat = FakeChat(lambda n: verdict("abort"))
    e = go(monkeypatch, ecfg, store, driver, sim, duration=6, chat=chat)
    assert e["state"] == "ABORTED" and e["abort_source"] == "safety_agent"
    assert e["abort_reason"] == "safety agent: home errors spreading"
    assert e["safety_checks"] == 1 and e["safety_decisions"][0]["decision"] == "abort"
    ledger = Ledger(store)
    assert ledger.get(other[0])["state"] == APPLIED
    assert [x["state"] for x in ledger.all() if x["experiment_id"] == e["id"]] == [REVERTED]
    row = [t for t in store.traces(10) if t["agent"] == "safety"][0]
    assert row["fallback"] == 0 and row["model"] == ecfg.llm.safety_model


def test_continue_does_not_prevent_watchdog_abort(monkeypatch, ecfg, store):
    sim = Sim()
    sim.bad = True
    chat = FakeChat(lambda n: verdict("continue", "fine", "low"))
    e = go(monkeypatch, ecfg, store, SimDriver(sim), sim, duration=6, chat=chat)
    assert e["state"] == "ABORTED" and e["abort_source"] == "watchdog" and e["abort_reason"].startswith("watchdog")


def test_continue_completes_and_records_checks(monkeypatch, ecfg, store):
    sim = Sim()
    chat = FakeChat(lambda n: verdict("continue", "stable", "low"))
    e = go(monkeypatch, ecfg, store, SimDriver(sim), sim, duration=3, chat=chat)
    assert e["state"] == "COMPLETED" and e["abort_source"] is None and e["safety_checks"] >= 2
    assert {c["decision"] for c in e["safety_decisions"]} == {"continue"}
    assert all(t["fallback"] == 0 for t in store.traces(20) if t["agent"] == "safety")


@pytest.mark.parametrize("content", ["not json", "{}", None])
def test_agent_failures_are_ignored(monkeypatch, ecfg, store, content):
    sim = Sim()

    class Boom(FakeChat):
        def invoke(self, msgs):
            raise TimeoutError("slow")

    chat = Boom(lambda n: "") if content is None else FakeChat(lambda n: content)
    e = go(monkeypatch, ecfg, store, SimDriver(sim), sim, duration=3, chat=chat)
    assert e["state"] == "COMPLETED" and e["safety_checks"] >= 1
    assert all(c["ignored"] and c["decision"] is None for c in e["safety_decisions"])
    traces = [t for t in store.traces(20) if t["agent"] == "safety"]
    assert traces and all(t["fallback"] == 1 for t in traces)


def test_budget_exhausted_is_ignored(monkeypatch, ecfg, store):
    store.add_usage(ecfg.llm.safety_model, 1, 100000, 100000)
    chat = FakeChat(lambda n: verdict("abort"))
    e = go(monkeypatch, ecfg, store, SimDriver(Sim()), Sim(), duration=2, chat=chat)
    assert e["state"] == "COMPLETED" and chat.calls == 0
    assert e["safety_decisions"][0]["error"] == "daily token budget reached"


def test_slow_agent_does_not_block_watchdog(monkeypatch, ecfg, store):
    sim = Sim()
    sim.bad = True
    logs = []
    chat = SlowChat(lambda n: verdict("continue", "ok", "low"), delay=2.5)
    ecfg.safety_agent.timeout_s = 4
    e = go(monkeypatch, ecfg, store, SimDriver(sim), sim, duration=8, chat=chat, log=lambda m: logs.append((time.time(), m)))
    abort_at = next(t for t, m in logs if "ABORT:" in m)
    assert e["abort_source"] == "watchdog"
    assert abort_at - e["injected_at"] < 2.0
    assert chat.calls == 1


def test_late_agent_answer_is_ignored(monkeypatch, ecfg, store):
    ecfg.safety_agent.timeout_s = 0.3
    chat = SlowChat(lambda n: verdict("abort"), delay=0.6)
    e = go(monkeypatch, ecfg, store, SimDriver(Sim()), Sim(), duration=2, chat=chat)
    assert e["state"] == "COMPLETED"
    assert e["safety_decisions"][0]["ignored"] and "timeout" in e["safety_decisions"][0]["error"]


def test_disabled_without_key_logs_once(monkeypatch, ecfg, store):
    logs = []
    for _ in range(2):
        e = go(monkeypatch, ecfg, store, SimDriver(Sim()), Sim(), duration=1, log=logs.append)
        assert e["state"] == "COMPLETED" and e["safety_checks"] == 0 and e["safety_decisions"] == []
    assert sum("safety agent disabled" in m for m in logs) == 1
    assert not [t for t in store.traces(20) if t["agent"] == "safety"]


def test_disabled_by_config(monkeypatch, ecfg, store):
    ecfg.safety_agent.enabled = False
    chat = FakeChat(lambda n: verdict("abort"))
    e = go(monkeypatch, ecfg, store, SimDriver(Sim()), Sim(), duration=2, chat=chat)
    assert e["state"] == "COMPLETED" and chat.calls == 0


def test_input_lists_journeys_outside_prediction(ecfg, store):
    base = bl.latest(store)
    now = time.time()
    rows = [{"journey": "cart", "ts_wall": now - 0.1 - i * 0.5, "latency_ms": 50.0, "ok": 0} for i in range(10)]
    rows += [{"journey": "home", "ts_wall": now - 0.1 - i * 0.5, "latency_ms": 50.0, "ok": 1} for i in range(10)]
    ctx = {"hypothesis": "h", "blast_radius": ["journey:home"], "predicted_journeys": ["home"]}
    text, outside = safety.build_input(ecfg, base, rows, "crash", "ad", {}, 5, 20, ctx, now)
    assert outside == ["cart"] and "cart: baseline err 0% p95 50ms; last10s n=10 err 100%" in text
    assert "outside the predicted blast radius: cart" in text and "elapsed 5s of 20s" in text
    _, none = safety.build_input(ecfg, base, rows, "crash", "ad", {}, 5, 20, None, now)
    assert none == []


def test_safety_out_validation():
    o = safety.SafetyOut.model_validate({"decision": "ABORT ", "reason": "x" * 500, "concern_level": "High"})
    assert o.decision == "abort" and len(o.reason) == 200 and o.concern_level == "high"
    with pytest.raises(Exception):
        safety.SafetyOut.model_validate({"decision": "maybe", "reason": "r", "concern_level": "low"})


def review_json(decision, reason="because", reduced=None):
    return json.dumps({"decision": decision, "reason": reason, **({"reduced_params": reduced} if reduced else {})})


def test_standalone_review_veto_skips_injection(monkeypatch, ecfg, store):
    sim, logs = Sim(), []
    driver = SimDriver(sim)
    chat = FakeChat(lambda n: review_json("veto", "repeats a known abort"))
    e = go(monkeypatch, ecfg, store, driver, sim, chat=chat, auto_review=True, log=logs.append)
    assert e["state"] == "VETOED" and chat.calls == 1 and not driver.applied and not Ledger(store).all()
    assert e["preflight_review"]["decision"] == "veto" and "repeats a known abort" in e["abort_reason"]
    assert any(m == "Safety review: vetoed: repeats a known abort" for m in logs)
    from faultline import memory

    assert memory.known_limits(ecfg, store) == []


def test_standalone_review_reduces_and_records(monkeypatch, ecfg, store):
    sim = Sim()
    chat = FakeChat(lambda n: review_json("reduce", "shorter is enough", {"duration_s": 10}) if n == 1 else verdict("continue", "ok", "low"))
    e = go(monkeypatch, ecfg, store, SimDriver(sim), sim, duration=11, chat=chat, auto_review=True)
    assert e["state"] == "COMPLETED" and e["params"]["duration_s"] == 10
    pf = e["preflight_review"]
    assert pf["decision"] == "reduce" and pf["original"]["duration_s"] == 11 and pf["final"]["duration_s"] == 10
    assert e["finished_at"] - e["injected_at"] < 14


def test_standalone_review_failure_approves_and_runs(monkeypatch, ecfg, store):
    sim = Sim()
    e = go(monkeypatch, ecfg, store, SimDriver(sim), sim, duration=2, chat=FakeChat(lambda n: "junk"), auto_review=True)
    assert e["state"] == "COMPLETED" and e["preflight_review"]["fallback"] == 1 and e["preflight_review"]["decision"] == "approve"


def test_review_not_called_unless_requested(monkeypatch, ecfg, store):
    chat = FakeChat(lambda n: review_json("veto"))
    e = go(monkeypatch, ecfg, store, SimDriver(Sim()), Sim(), chat=chat)
    assert e["state"] == "COMPLETED" and e["preflight_review"] is None
