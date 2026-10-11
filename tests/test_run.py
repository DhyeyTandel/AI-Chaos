import json
import re
import time

import pytest

from faultline import graph as plan_graph
from faultline import runs
from faultline.agents import planner
from test_planner import FakeChat, fresh_limiter, gmap  # noqa: F401


@pytest.fixture
def rcfg(cfg):
    cfg.run.cooldown_s = 0
    return cfg


class Script:
    def __init__(self, outcomes=None):
        self.outcomes = outcomes or {}
        self.calls = []
        self.n = 0

    def __call__(self, c, context=None, review=None):
        self.calls.append(c)
        self.reviews = [*getattr(self, 'reviews', []), review]
        self.n += 1
        state, reason = self.outcomes.get(self.n, ("COMPLETED", None))
        return {"id": f"exp-{self.n}", "state": state, "abort_reason": reason, "recovery_seconds": None if reason else 2.5}


class Control:
    def __init__(self):
        self.why = None

    def reason(self):
        return self.why


def go(cfg, store, rounds, runner, settler=lambda: ("ok", ""), control=None, chat=None):
    return plan_graph.run_loop(cfg, gmap(), store, rounds, runner, settler, control or Control(), chat=chat, log=lambda m: None)


def test_tier_assignment(cfg):
    tiers = {c.id: c.tier for c in planner.generate_candidates(cfg, gmap())}
    assert tiers["delay:cart:50ms"] == 1 and tiers["delay:cart:300ms"] == 2 and tiers["loss:cart:10pct"] == 2
    assert tiers["crash:quote:20s"] == 1 and tiers["crash:checkout:20s"] == 1
    assert tiers["crash:cart:20s"] == 2
    assert tiers["crash:frontend:20s"] == 3 and tiers["crash:valkey-cart:20s"] == 3


def test_tier_overrides_and_critical(cfg):
    cfg.planner.tiers.critical = ["quote"]
    setattr(cfg.planner.tiers.overrides, "crash:cart:20s", 3)
    setattr(cfg.planner.tiers.overrides, "delay:cart:50ms", 2)
    tiers = {c.id: c.tier for c in planner.generate_candidates(cfg, gmap())}
    assert tiers["crash:quote:20s"] == 3 and tiers["crash:cart:20s"] == 3 and tiers["delay:cart:50ms"] == 2


def test_climbs_after_two_survived(rcfg, store):
    run = go(rcfg, store, 3, Script())
    rows = runs.rounds(store, run["run_id"])
    assert [r["tier"] for r in rows] == [1, 1, 2]
    assert rows[0]["climb_decision"] == "no climb: 1/2 survived at tier 1"
    assert rows[1]["climb_decision"] == "climb 1->2: 2 survived at tier 1"
    assert rows[2]["climb_decision"] == "no climb: 1/2 survived at tier 2"
    assert run["final_tier"] == 2 and run["rounds_done"] == 3 and run["stop_reason"] == "completed 3 rounds"


def test_climb_caps_at_tier_three(rcfg, store):
    rcfg.run.climb_after = 1
    run = go(rcfg, store, 4, Script())
    rows = runs.rounds(store, run["run_id"])
    assert [r["tier"] for r in rows] == [1, 2, 3, 3]
    assert rows[3]["climb_decision"] == "no climb: already at max tier 3"
    assert run["final_tier"] == 3


def test_abort_does_not_climb_and_excludes_target(rcfg, store):
    script = Script({1: ("ABORTED", "watchdog breach: home p95")})
    run = go(rcfg, store, 3, script)
    rows = runs.rounds(store, run["run_id"])
    first = rows[0]
    assert first["outcome"] == "ABORTED" and first["climb_decision"].startswith("no climb: aborted")
    assert first["target"] in first["climb_decision"]
    assert [r["tier"] for r in rows] == [1, 1, 1]
    assert all(r["target"] != first["target"] for r in rows[1:])
    assert len({r["target"] for r in rows}) == 3


def test_targets_not_repeated_in_run(rcfg, store):
    run = go(rcfg, store, 8, Script())
    targets = [r["target"] for r in runs.rounds(store, run["run_id"])]
    assert len(set(targets)) == len(targets)


def test_stops_when_recovery_wait_fails(rcfg, store):
    script = Script()
    run = go(rcfg, store, 3, script, settler=lambda: ("timeout", "home p95 too high"))
    assert len(script.calls) == 1
    assert run["stop_reason"] == "system did not return to baseline" and run["rounds_done"] == 1


def test_kill_switch_ends_whole_run(rcfg, store, tmp_path):
    sentinel = tmp_path / "STOP"
    control = runs.RunControl(path=sentinel)

    def runner(c, context=None, review=None):
        sentinel.write_text("1")
        return {"id": "exp-x", "state": "ABORTED", "abort_reason": "kill switch (STOP file)", "recovery_seconds": None}

    called = []
    run = go(rcfg, store, 3, runner, settler=lambda: called.append(1) or ("ok", ""), control=control)
    assert run["rounds_done"] == 1 and run["stop_reason"] == "kill switch (STOP file)" and not called
    assert runs.rounds(store, run["run_id"])[0]["climb_decision"] == "no climb: run stopped by operator"


def test_kill_switch_before_plan_runs_nothing(rcfg, store, tmp_path):
    sentinel = tmp_path / "STOP"
    sentinel.write_text("1")
    script = Script()
    run = go(rcfg, store, 3, script, control=runs.RunControl(path=sentinel))
    assert not script.calls and run["rounds_done"] == 0 and "kill switch" in run["stop_reason"]


def test_kill_switch_during_settle(rcfg, store):
    control = Control()
    script = Script()

    def settler():
        control.why = "operator interrupt (signal)"
        return "stopped", ""

    run = go(rcfg, store, 3, script, settler=settler, control=control)
    assert len(script.calls) == 1 and run["stop_reason"] == "operator interrupt (signal)"


def test_run_control_thread_latches_sentinel(tmp_path):
    sentinel = tmp_path / "STOP"
    with runs.RunControl(path=sentinel, poll_s=0.01) as c:
        assert c.reason() is None
        sentinel.write_text("1")
        time.sleep(0.1)
        sentinel.unlink()
        assert c.reason() == "kill switch (STOP file)"


class Spy(FakeChat):
    def __init__(self):
        super().__init__(lambda n: "not json")
        self.users = []

    def invoke(self, msgs):
        self.users.append(msgs[1][1])
        return super().invoke(msgs)


def test_planner_gets_only_tier_eligible_candidates(rcfg, store):
    chat = Spy()
    tier_of = {c.id: c.tier for c in planner.generate_candidates(rcfg, gmap())}
    script = Script()
    go(rcfg, store, 3, script, chat=chat)
    firsts = list(dict.fromkeys(u for u in chat.users if "Strategy: survivable first" in u))
    assert len(firsts) == 3
    max_tier = [1, 1, 2]
    tested = []
    for user, mt, picked in zip(firsts, max_tier, script.calls):
        ids = re.findall(r"^\d+\. (\S+) \(target (\S+),", user, re.M)
        assert ids and all(tier_of[i] <= mt for i, _ in ids)
        assert all(t not in tested for _, t in ids)
        assert "Strategy: survivable first" in user and f"Current tier {mt}" in user
        tested.append(picked.target)
    assert "Results so far: none yet" in firsts[0] and script.calls[0].id in firsts[1]


def test_db_rows_written(rcfg, store):
    chat = FakeChat(lambda n: "bad")
    run = go(rcfg, store, 2, Script({2: ("ABORTED", "watchdog breach")}), chat=chat)
    assert run["ended_at"] and run["rounds_planned"] == 2 and run["rounds_done"] == 2
    assert runs.get(store)["run_id"] == run["run_id"] == runs.get(store, run["run_id"])["run_id"]
    rows = runs.rounds(store, run["run_id"])
    assert [r["round"] for r in rows] == [1, 2]
    r = rows[0]
    assert r["fallback"] == 1 and r["reason"] and r["hypothesis"] and r["experiment_id"] == "exp-1"
    assert r["fault"] in ("crash", "delay") and isinstance(r["params"], dict) and isinstance(r["predicted_blast_radius"], list)
    assert rows[1]["outcome"] == "ABORTED" and rows[1]["abort_reason"] == "watchdog breach" and rows[0]["recovery_s"] == 2.5


def test_inject_node_plans_before_execute(rcfg, store):
    lines = []
    plan_graph.run_loop(rcfg, gmap(), store, 1, Script(), lambda: ("ok", ""), Control(), log=lines.append)
    i = next(n for n, m in enumerate(lines) if "Injection agent plan:" in m)
    assert "running " in lines[i + 1]


def test_inject_node_stops_run_on_invalid_plan(rcfg, store, monkeypatch):
    from faultline.agents import injection
    from faultline.faults import ValidationError

    def bad(*a, **k):
        raise ValidationError("nope")

    monkeypatch.setattr(injection, "prepare", bad)
    s = Script()
    run = go(rcfg, store, 2, s)
    assert run["stop_reason"] == "experiment rejected: nope" and s.calls == []
