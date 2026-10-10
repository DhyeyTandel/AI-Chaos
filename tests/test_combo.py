import asyncio
import json
import time

import pytest
from typer.testing import CliRunner

from faultline import analyzer, combo, faults, memory, reaper, runs
from faultline import experiment as exp_mod
from faultline import graph as plan_graph
from faultline.agents import planner, safety, validator
from faultline.cli import app
from faultline.config import load_config
from faultline.injector import Injector
from faultline.ledger import APPLIED, PENDING, REVERTED, Ledger
from conftest import FakeDriver
from test_experiment import JOURNEYS, Sim, SimDriver, ecfg  # noqa: F401
from test_memory import add_abort
from test_planner import FakeChat, fresh_limiter, gmap  # noqa: F401
from test_report import add_exp, add_run, jr, seed_baseline
from test_review import reply, seed


@pytest.fixture(autouse=True)
def no_cache(monkeypatch):
    monkeypatch.setenv("FAULTLINE_LLM_CACHE", "0")


SPIKE = {"fault": "spike", "target": "traffic", "params": {"users": 100, "spawn_rate": 25}}
DELAY = {"fault": "delay", "target": "quote", "params": {"delay_ms": 50}}
DELAY2 = {"fault": "delay", "target": "cart", "params": {"delay_ms": 50}}
CRASH = {"fault": "crash", "target": "ad", "params": {}}
CRASH2 = {"fault": "crash", "target": "cart", "params": {}}
LOSS = {"fault": "delay", "target": "quote", "params": {"delay_ms": 1, "loss_pct": 10}}


def pp(*parts):
    return {"parts": [dict(p) for p in parts]}


def make(store, driver, cfg):
    return Ledger(store), Injector(Ledger(store), driver, cfg, retry_delay=0)


@pytest.mark.parametrize("parts, msg", [
    ([DELAY], "at least 2"),
    ([SPIKE, DELAY, DELAY2], "at most 2 parts"),
    ([CRASH, CRASH2], "at most one crash"),
    ([DELAY, {**DELAY2, "target": "quote"}], "different things"),
    ([SPIKE, {**SPIKE, "params": {"users": 60}}], "different things"),
    ([SPIKE, {"fault": "crash", "target": "kafka", "params": {}}], "may not be crashed inside a combo"),
    ([DELAY, {"fault": "crash", "target": "frontend-proxy", "params": {}}], "part 2"),
    ([{**DELAY, "params": {"delay_ms": 5000}}, SPIKE], "part 1"),
    ([DELAY, {"fault": "delay", "target": "prober", "params": {}}], "allowlist"),
    ([SPIKE, {**DELAY, "target": "traffic"}], "allowlist"),
    ([{**SPIKE, "params": {"users": 1000}}, DELAY], "users must be"),
    ([CRASH, LOSS], "not an allowed combination"),
    ([SPIKE, {"fault": "bogus", "target": "cart", "params": {}}], "unknown fault"),
])
def test_validation_refuses(cfg, parts, msg):
    with pytest.raises(faults.ValidationError, match=msg):
        faults.validate(cfg, "combo", "x", 30, pp(*parts))


def test_validation_accepts_allowed_pairs_in_apply_order(cfg):
    out = faults.validate(cfg, "combo", "x", 30, pp(DELAY, SPIKE))
    assert [p["fault"] for p in out["parts"]] == ["spike", "delay"]
    assert combo.target_key(out["parts"]) == "traffic+quote"
    assert out["parts"][0]["params"]["users"] == 100 and out["parts"][0]["params"]["duration_s"] == 30
    assert out["threshold_factors"] == {"p95": 0.8, "error": 0.8}
    assert combo.target_key(faults.validate(cfg, "combo", "x", 30, pp(CRASH, DELAY))["parts"]) == "quote+ad"
    for a, b in ((SPIKE, LOSS), (DELAY, DELAY2), (SPIKE, CRASH)):
        faults.validate(cfg, "combo", "x", 30, pp(a, b))
    again = faults.validate(cfg, "combo", "x", 30, {"parts": out["parts"]})
    assert again["parts"] == out["parts"]


def test_validation_duration_cap_and_switch(cfg):
    with pytest.raises(faults.ValidationError, match="duration"):
        faults.validate(cfg, "combo", "x", 500, pp(SPIKE, DELAY))
    cfg.combined.enabled = False
    with pytest.raises(faults.ValidationError, match="disabled"):
        faults.validate(cfg, "combo", "x", 30, pp(SPIKE, DELAY))


def test_pairs_come_from_config(cfg):
    cfg.combined.allowed_pairs = ["crash+delay"]
    faults.validate(cfg, "combo", "x", 30, pp(DELAY, CRASH))
    with pytest.raises(faults.ValidationError, match="not an allowed"):
        faults.validate(cfg, "combo", "x", 30, pp(SPIKE, DELAY))
    cfg.combined.allowed_pairs = ["spike+delay", "delay+delay"]
    cfg.combined.max_parts = 3
    with pytest.raises(faults.ValidationError, match="not an allowed"):
        faults.validate(cfg, "combo", "x", 30, pp(DELAY, DELAY2, CRASH))


def test_parse_cli_syntax():
    assert combo.parse("spike:traffic:users=100") == {"fault": "spike", "target": "traffic", "params": {"users": 100.0}}
    assert combo.parse("delay:quote:delay_ms=50,jitter_ms=5")["params"] == {"delay_ms": 50.0, "jitter_ms": 5.0}
    assert combo.parse("crash:cart") == {"fault": "crash", "target": "cart", "params": {}}
    assert combo.parse("loss:quote:loss_pct=10") == {"fault": "delay", "target": "quote", "params": {"loss_pct": 10.0, "delay_ms": 1.0}}
    for bad in ("delay", "bogus:quote", "delay:quote:nope=1", "delay:quote:delay_ms=x", "crash:cart:delay_ms=5", "loss:quote", ":quote"):
        with pytest.raises(faults.ValidationError):
            combo.parse(bad)


def test_tighten_multiplies_limits(cfg):
    th = {"home": {"error_rate_max": 0.2, "p95_max_ms": 800}}
    assert combo.tighten(cfg, th) == {"home": {"error_rate_max": pytest.approx(0.16), "p95_max_ms": pytest.approx(640)}}
    assert th["home"]["p95_max_ms"] == 800


def test_all_parts_pending_before_any_apply_in_fixed_order(store, driver, cfg):
    ledger, inj = make(store, driver, cfg)
    seen = []
    base_apply = driver.apply

    def apply(entry):
        seen.append((entry["kind"], [e["state"] for e in ledger.all()]))
        return base_apply(entry)

    driver.apply = apply
    ids = inj.inject_combo("e1", pp(DELAY, SPIKE), 10)
    assert [k for k, _ in seen] == ["spike", "delay"]
    assert seen[0][1] == [PENDING, PENDING]
    assert seen[1][1] == [APPLIED, PENDING]
    rows = [ledger.get(i) for i in ids]
    assert {r["experiment_id"] for r in rows} == {"e1"} and [r["state"] for r in rows] == [APPLIED, APPLIED]
    assert rows[0]["expires_at"] == rows[1]["expires_at"]
    assert inj.revert_experiment("e1") == {ids[0]: REVERTED, ids[1]: REVERTED}


def test_crash_is_applied_last(store, driver, cfg):
    ledger, inj = make(store, driver, cfg)
    order = []
    base_apply = driver.apply
    driver.apply = lambda e: (order.append(e["kind"]), base_apply(e))[1]
    inj.inject_combo("e1", pp(CRASH, DELAY), 10)
    assert order == ["delay", "crash"]


def test_second_part_failure_reverts_first(store, driver, cfg):
    ledger, inj = make(store, driver, cfg)
    base_apply = driver.apply
    calls = []

    def apply(entry):
        calls.append(entry["id"])
        if len(calls) == 2:
            raise RuntimeError("no docker")
        return base_apply(entry)

    driver.apply = apply
    with pytest.raises(RuntimeError):
        inj.inject_combo("e1", pp(SPIKE, DELAY), 10)
    assert [e["state"] for e in ledger.all()] == [REVERTED, REVERTED]
    assert driver.applied == set() and ledger.open_entries() == []


def test_combo_refused_while_faults_open(store, driver, cfg):
    ledger, inj = make(store, driver, cfg)
    inj.inject("e0", "crash", "cart", {}, 10)
    with pytest.raises(faults.ValidationError):
        inj.inject_combo("e1", pp(SPIKE, DELAY), 10)
    assert len(ledger.all()) == 1


def test_reaper_reverts_combo_parts(store, driver, cfg):
    ledger, inj = make(store, driver, cfg)
    ids = inj.inject_combo("e1", pp(SPIKE, DELAY), 100)
    ledger.beat("e1")
    now = time.time()
    assert reaper.tick(ledger, inj, cfg, now=now + 1) == {}
    assert reaper.tick(ledger, inj, cfg, now=now + cfg.reaper.stale_s + 1) == {ids[0]: REVERTED, ids[1]: REVERTED}
    ids = inj.inject_combo("e2", pp(SPIKE, DELAY), 10)
    ledger.beat("e2")
    assert set(reaper.tick(ledger, inj, cfg, now=time.time() + 100)) == set(ids)


class Sim2(Sim):
    latency = 50.0


def run(monkeypatch, ecfg, store, driver, sim, fault, target, params, duration=2, **kw):
    async def fake_probe(st, seconds, timeout=3.0, stop=None):
        while not stop.is_set():
            t = time.time()
            ok = not (sim.faulted and sim.bad)
            lat = sim.latency if sim.faulted else 50.0
            st.insert_many([(t, 0.0, j, ok, 200, lat, None) for j in JOURNEYS])
            await asyncio.sleep(0.05)

    monkeypatch.setattr(exp_mod, "run_probe", fake_probe)
    return asyncio.run(exp_mod.run_experiment(store, driver, ecfg, fault, target, duration, params, log=lambda m: None, **kw))


def states(store, exp_id):
    return [e["state"] for e in Ledger(store).all() if e["experiment_id"] == exp_id]


def test_combo_completes_and_is_recorded(monkeypatch, ecfg, store):
    sim = Sim2()
    e = run(monkeypatch, ecfg, store, SimDriver(sim), sim, "combo", "ignored", pp(DELAY, SPIKE))
    assert e["state"] == "COMPLETED" and e["fault"] == "combo" and e["target"] == "traffic+quote"
    assert [p["fault"] for p in e["params"]["parts"]] == ["spike", "delay"] and e["params"]["threshold_factors"]["p95"] == 0.8
    assert states(store, e["id"]) == [REVERTED, REVERTED] and e["impact"] and e["recovery_seconds"] is not None
    assert analyzer.fault_label(e["fault"], e["params"]) == "combo: spike 100 users + delay quote 50 ms"


def test_watchdog_abort_reverts_all_parts_and_leaves_no_open_entries(monkeypatch, ecfg, store):
    sim = Sim2()
    sim.bad = True
    e = run(monkeypatch, ecfg, store, SimDriver(sim), sim, "combo", "x", pp(SPIKE, DELAY), duration=10)
    assert e["state"] == "ABORTED" and e["abort_source"] == "watchdog"
    assert states(store, e["id"]) == [REVERTED, REVERTED] and Ledger(store).open_entries() == []
    assert not sim.faulted


def test_kill_switch_reverts_all_parts(monkeypatch, ecfg, store):
    sim = Sim2()
    driver = SimDriver(sim, on_apply=lambda en: exp_mod.stop_path().write_text("1"))
    e = run(monkeypatch, ecfg, store, driver, sim, "combo", "x", pp(SPIKE, DELAY), duration=10)
    exp_mod.stop_path().unlink(missing_ok=True)
    assert e["state"] == "ABORTED" and e["abort_source"] == "operator" and "kill switch" in e["abort_reason"]
    assert states(store, e["id"]) == [REVERTED, REVERTED] and driver.applied == set()


def test_partial_apply_failure_in_run(monkeypatch, ecfg, store):
    sim = Sim2()
    n = []

    def on_apply(entry):
        n.append(1)
        if len(n) == 2:
            raise RuntimeError("boom")

    driver = SimDriver(sim, on_apply=on_apply)
    e = run(monkeypatch, ecfg, store, driver, sim, "combo", "x", pp(SPIKE, DELAY))
    assert e["state"] == "ABORTED" and e["abort_source"] == "inject" and e["abort_reason"].startswith("inject failed")
    assert states(store, e["id"]) == [REVERTED, REVERTED] and driver.applied == set() and not sim.faulted


def test_combo_uses_tighter_limits_than_single(monkeypatch, ecfg, store):
    sim = Sim2()
    sim.latency = 700.0
    single = run(monkeypatch, ecfg, store, SimDriver(sim), sim, "crash", "ad", {}, duration=3)
    assert single["state"] == "COMPLETED"
    time.sleep(1.5)
    both = run(monkeypatch, ecfg, store, SimDriver(sim), sim, "combo", "x", pp(SPIKE, DELAY), duration=3)
    assert both["state"] == "ABORTED" and both["abort_source"] == "watchdog" and "p95" in both["abort_reason"] and "640" in both["abort_reason"]


def test_invalid_combo_applies_nothing(monkeypatch, ecfg, store):
    sim = Sim2()
    driver = SimDriver(sim)
    with pytest.raises(faults.ValidationError):
        run(monkeypatch, ecfg, store, driver, sim, "combo", "x", pp(CRASH, CRASH2))
    assert Ledger(store).all() == [] and driver.applied == set()
    assert store.conn.execute("SELECT COUNT(*) FROM experiments").fetchone()[0] == 0


def test_cli_refuses_two_crashes(tmp_path, monkeypatch):
    monkeypatch.setenv("FAULTLINE_DB", str(tmp_path / "c.db"))
    r = CliRunner().invoke(app, ["experiment", "--combo", "crash:cart", "--combo", "crash:ad", "--duration", "10"])
    assert r.exit_code == 2 and "at most one crash" in r.output
    r = CliRunner().invoke(app, ["experiment", "--combo", "spike:traffic:users=100", "--fault", "crash", "--duration", "10"])
    assert r.exit_code == 2 and "replaces" in r.output
    assert not (tmp_path / "c.db").exists()


def combo_review(cfg, store, content, parts=None):
    base = seed(store)
    norm = safety._norm(cfg, "combo", "traffic+quote", 30, pp(*(parts or [SPIKE, DELAY])))
    return safety.preflight_review(cfg, store, base, "combo", "traffic+quote", 30, norm, 3, None, gmap(), FakeChat(lambda n: content))


def test_review_can_lower_any_part_only(cfg, store):
    r = combo_review(cfg, store, reply("reduce", "heavy", {"duration_s": 20, "parts": [{"index": 1, "users": 60}, {"index": 2, "delay_ms": 30}]}))
    assert r["decision"] == "reduce" and r["final"]["duration_s"] == 20
    fin = {p["target"]: p["params"] for p in r["final"]["params"]["parts"]}
    assert fin["traffic"]["users"] == 60 and fin["quote"]["delay_ms"] == 30
    assert safety.describe_review(r) == "reduced traffic users 100 -> 60 users, quote delay 50 -> 30 ms, duration 30 -> 20 s: heavy"
    assert faults.validate(cfg, "combo", "x", r["final"]["duration_s"], r["final"]["params"])["parts"][0]["params"]["users"] == 60


@pytest.mark.parametrize("reduced", [
    {"parts": [{"index": 2, "delay_ms": 80}]}, {"parts": [{"index": 3, "users": 10}]}, {"parts": [{"users": 10}]},
    {"parts": [{"index": 1, "duration_s": 10}]}, {"parts": [{"index": 1, "delay_ms": 10}]}, {"duration_s": 40}, {"parts": []},
])
def test_review_raise_or_invalid_vetoes_whole_combo(cfg, store, reduced):
    r = combo_review(cfg, store, reply("reduce", "try", reduced))
    assert r["decision"] == "veto" and r["final"] is None and "reduction rejected" in r["reason"]


def test_review_veto_and_crash_part_not_lowerable(cfg, store):
    assert combo_review(cfg, store, reply("veto", "too risky"))["decision"] == "veto"
    r = combo_review(cfg, store, reply("reduce", "x", {"parts": [{"index": 1, "delay_ms": 10}]}), parts=[DELAY, CRASH])
    assert r["decision"] == "reduce" and r["final"]["params"]["parts"][1]["params"] == {}
    bad = combo_review(cfg, store, reply("reduce", "x", {"parts": [{"index": 2, "users": 10}]}), parts=[DELAY, CRASH])
    assert bad["decision"] == "veto"


def vparts(*parts):
    return faults.validate(load_config(path="/nonexistent"), "combo", "x", 30, pp(*parts))["parts"]


def test_memory_records_combo_limit_keyed_by_sorted_pair(cfg, store):
    parts = vparts(SPIKE, DELAY)
    add_abort(store, "exp-c1", "traffic+quote", fault="combo", params={"parts": parts, "duration_s": 30})
    add_abort(store, "exp-c2", "quote+traffic", fault="combo", params={"parts": list(reversed(parts)), "duration_s": 30}, source="operator")
    assert memory.known_limits(cfg, store) == []
    lims = memory.combo_limits(cfg, store)
    assert len(lims) == 1 and lims[0]["key"] == "delay:quote + spike:traffic" and lims[0]["experiment_id"] == "exp-c1"
    assert {p["kind"]: p["intensity"] for p in lims[0]["parts"]} == {"delay": 50.0, "spike": 100.0}
    cfg.planner.memory.enabled = False
    assert memory.combo_limits(cfg, store) == []


def test_combo_limit_keeps_lowest_and_does_not_touch_singles(cfg, store):
    hi = vparts(SPIKE, DELAY)
    lo = vparts({**SPIKE, "params": {"users": 60, "spawn_rate": 25}}, DELAY)
    add_abort(store, "exp-hi", "traffic+quote", fault="combo", params={"parts": hi}, source="safety_agent")
    add_abort(store, "exp-lo", "traffic+quote", fault="combo", params={"parts": lo}, source="watchdog")
    assert [x["experiment_id"] for x in memory.combo_limits(cfg, store)] == ["exp-lo"]
    ids, res = planner_ids(cfg, store)
    assert "delay:quote:50ms" in ids and "spike:traffic:100users" in ids and "spike:traffic:50users" in ids and not res.exclusions


def planner_ids(cfg, store, **kw):
    cfg.planner.max_per_target = 99
    res = planner.plan(cfg, gmap(), store, 100, chat=FakeChat(lambda n: "bad"), **kw)
    return [s.candidate.id for s in res.ranked], res


def add_done(store, eid, fault, target, params, state="COMPLETED"):
    with store.conn:
        store.conn.execute(
            "INSERT INTO experiments (id, fault, target, params, state, created_at, injected_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (eid, fault, target, json.dumps(params), state, time.time(), time.time()),
        )


def survive(store):
    add_done(store, "s1", "spike", "traffic", {"duration_s": 30, "users": 100, "spawn_rate": 25.0})
    add_done(store, "s2", "delay", "quote", {"duration_s": 20, "delay_ms": 50.0, "jitter_ms": 0.0, "loss_pct": 0.0})
    add_done(store, "s3", "delay", "cart", {"duration_s": 20, "delay_ms": 50.0, "jitter_ms": 0.0, "loss_pct": 0.0})
    add_done(store, "s4", "crash", "valkey-cart", {"duration_s": 20})
    add_done(store, "s5", "delay", "frontend", {"duration_s": 20, "delay_ms": 300.0, "jitter_ms": 0.0, "loss_pct": 0.0}, state="ABORTED")


def combo_ids(ids):
    return sorted(i for i in ids if i.startswith("combo:"))


def test_planner_has_no_combos_without_survivors(cfg, store):
    ids, _ = planner_ids(cfg, store, max_tier=3)
    assert combo_ids(ids) == []


def test_planner_combos_only_at_tier_3_from_survived_singles(cfg, store):
    survive(store)
    for tier in (1, 2):
        assert combo_ids(planner_ids(cfg, store, max_tier=tier)[0]) == []
    want = [
        "combo:delay:cart:50ms+delay:quote:50ms", "combo:spike:traffic:100users+delay:cart:50ms",
        "combo:spike:traffic:100users+delay:quote:50ms",
    ]
    ids, res = planner_ids(cfg, store, max_tier=3)
    assert combo_ids(ids) == sorted(want)
    assert combo_ids(planner_ids(cfg, store)[0]) == sorted(want)
    c = next(s.candidate for s in res.ranked if s.candidate.id == want[2])
    assert c.tier == 3 and c.fault == "combo" and c.target == "traffic+quote" and c.label == "combo: spike 100 users + delay quote 50 ms"
    assert validator.validate_candidate(cfg, store, c) == (True, "ok")
    assert [s.candidate.id for s in res.ranked] == [s.candidate.id for s in planner.plan(cfg, gmap(), store, 100, chat=FakeChat(lambda n: "bad"), max_tier=3).ranked]


def test_planner_combo_gates_and_cap(cfg, store):
    survive(store)
    cfg.combined.planner = False
    assert combo_ids(planner_ids(cfg, store, max_tier=3)[0]) == []
    cfg.combined.planner, cfg.combined.enabled = True, False
    assert combo_ids(planner_ids(cfg, store, max_tier=3)[0]) == []
    cfg.combined.enabled, cfg.combined.max_per_plan = True, 2
    assert len(combo_ids(planner_ids(cfg, store, max_tier=3)[0])) == 2


def test_planner_combo_abort_excludes_only_that_combo(cfg, store):
    survive(store)
    add_abort(store, "exp-x", "traffic+quote", fault="combo", params={"parts": vparts(SPIKE, DELAY)})
    ids, res = planner_ids(cfg, store, max_tier=3)
    assert "combo:spike:traffic:100users+delay:quote:50ms" not in ids and "combo:spike:traffic:100users+delay:cart:50ms" in ids
    assert "delay:quote:50ms" in ids and "spike:traffic:100users" in ids
    ex = [x for x in res.exclusions if x["candidate"].startswith("combo:")]
    assert len(ex) == 1 and ex[0]["evidence"] == "exp-x" and "aborted in exp-x" in ex[0]["reason"]


def sample_combo_run(cfg, store, parts, impact, target=None):
    seed_baseline(store, cfg)
    vp = vparts(*parts)
    add_exp(store, "ec", target or combo.target_key(vp), fault="combo", params={"parts": vp, "duration_s": 30}, impact=impact, recovery=3.0)
    return add_run(store, [{"target": target or combo.target_key(vp), "fault": "combo", "experiment_id": "ec", "candidate_id": "combo:x"}])


def test_report_labels_and_lists_parts(cfg, store, tmp_path, monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    run = sample_combo_run(cfg, store, [SPIKE, DELAY], {"home": jr(p95=500.0), "add_to_cart": jr(err=0.4)})
    an = analyzer.analyze(cfg, store, gmap(), run, runs.rounds(store, "run-test"))
    a = an["experiments"][0]
    assert a["label"] == "combo: spike 100 users + delay quote 50 ms" and a["combo"] and a["target"] == "traffic+quote"
    assert [p["label"] for p in a["parts"]] == ["spike 100 users", "delay quote 50 ms"]
    assert a["impact"] > 0 and a["breadth"] > 0 and an["services"]["traffic+quote"]["experiments"] == 1
    plan_graph.run_report(cfg, gmap(), store, tmp_path, "run-test")
    md = (tmp_path / "run-test" / "report.md").read_text()
    assert "combo: spike 100 users + delay quote 50 ms" in md and "## Combined failures" in md
    assert "1. spike 100 users; 2. delay quote 50 ms" in md and "\u2014" not in md


def test_hidden_dependency_is_attributed_to_the_combo(cfg, store):
    run = sample_combo_run(cfg, store, [DELAY, DELAY2], {"home": jr(p95=900.0), "add_to_cart": jr()})
    an = analyzer.analyze(cfg, store, gmap(), run, runs.rounds(store, "run-test"))
    a = an["experiments"][0]
    assert a["mapped_journeys"] == ["add_to_cart"] and a["hidden_journeys"] == ["home"]
    h = an["hidden"][0]
    assert h["target"] == "cart+quote" and h["fault"].startswith("combo: ") and "delay quote 50 ms" in h["fault"]
    assert "quote" not in an["services"] and "cart" not in an["services"]


def test_label_for_singles_is_unchanged():
    assert analyzer.fault_label("delay", {"delay_ms": 50}) == "delay 50ms"
    assert analyzer.mapped_journeys(gmap(), "traffic+cart") == {"add_to_cart", "home"}


def test_live_check_prompt_describes_whole_combo(cfg, store):
    base = seed(store)
    params = faults.validate(cfg, "combo", "x", 30, pp(SPIKE, DELAY))
    text, _ = safety.build_input(cfg, {**base, "thresholds": combo.tighten(cfg, base["thresholds"])}, [], "combo", "traffic+quote", params, 5, 30, None, time.time())
    assert "Fault: combo: spike 100 users + delay quote 50 ms (simultaneous parts)" in text and "p95 640ms" in text
