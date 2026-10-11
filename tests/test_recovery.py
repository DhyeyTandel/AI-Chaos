import asyncio
import json

import pytest

from faultline import analyzer, report
from faultline import experiment as exp_mod
from faultline.agents import recovery
from faultline.ledger import Ledger
from test_experiment import Sim, SimDriver, ecfg, go as exp_go  # noqa: F401
from test_planner import FakeChat, fresh_limiter  # noqa: F401

GRAPH = {
    "nodes": {},
    "journeys": {
        "home": {"services": ["frontend", "ad", "kafka"]},
        "cart": {"services": ["frontend", "cart", "valkey-cart", "astronomy-db", "jaeger", "prometheus", "load-generator"]},
    },
}


class FakeChat(FakeChat):
    def invoke(self, msgs):
        self.last_user = msgs[-1][1] if isinstance(msgs[-1], tuple) else str(msgs[-1])
        return super().invoke(msgs)


def verdict(action, service=None, diagnosis="ad is not running", reason="restart it"):
    return json.dumps({"diagnosis": diagnosis, "action": action, "service": service, "reason": reason})


class Stuck(SimDriver):
    """After the revert the journeys stay broken until a container is restarted (an external actor stopped a service)."""

    def revert(self, entry):
        super().revert(entry)
        self.sim.bad = True
        self.sim.faulted = True

    def restart_container(self, name):
        super().restart_container(name)
        if name in self.fixes:
            self.sim.faulted = False

    fixes = ("ad",)


@pytest.fixture
def rcfg(ecfg):
    ecfg.recovery_agent.settle_s = 0.3
    ecfg.recovery_agent.remeasure_s = 3
    ecfg.recovery_agent.timeout_s = 2
    return ecfg


def run(monkeypatch, rcfg, store, driver, sim, chat=None, log=None, **kw):
    return exp_go(monkeypatch, rcfg, store, driver, sim, duration=1, chat=chat, log=log or (lambda m: None), graph=GRAPH, **kw)


def actions(store, exp_id):
    return store.recovery_actions(exp_id)


def test_no_llm_call_when_recovered(monkeypatch, rcfg, store):
    sim = Sim()
    chat = FakeChat(lambda n: verdict("restart_service", "ad"))
    lines = []
    e = run(monkeypatch, rcfg, store, SimDriver(sim), sim, chat, lines.append)
    ra = e["recovery_agent"]
    assert e["recovery_seconds"] is not None and ra["outcome"] == "not needed" and ra["initial"] == "recovered" and ra["actions"] == []
    assert chat.calls == 0 and actions(store, e["id"]) == []
    assert any("Recovery agent: recovered, no action" in x for x in lines)
    row = [t for t in store.traces(30) if t["agent"] == "recovery"][0]
    assert row["model"] == "deterministic" and row["fallback"] == 0 and "recovered in" in json.loads(row["output_json"])["note"]


def test_failed_recovery_restart_service_then_remeasure(monkeypatch, rcfg, store):
    sim = Sim()
    d = Stuck(sim)
    chat = FakeChat(lambda n: verdict("restart_service", "ad"))
    lines = []
    e = run(monkeypatch, rcfg, store, d, sim, chat, lines.append)
    ra = e["recovery_agent"]
    assert d.restarted == ["ad"] and chat.calls == 1
    assert ra["outcome"] == "remediated" and ra["initial"] == "failed" and len(ra["actions"]) == 1
    assert e["recovery_seconds"] is not None and e["recovery_seconds"] >= rcfg.recovery_agent.settle_s
    assert e["recovery_seconds"] > rcfg.recovery.cap_s - 0.5
    rows = actions(store, e["id"])
    assert [(r["step"], r["action"], r["service"]) for r in rows] == [(1, "restart_service", "ad")]
    assert rows[0]["diagnosis"] == "ad is not running" and rows[0]["result"] == "restarted ad" and rows[0]["ts"]
    traces = [t for t in store.traces(50) if t["agent"] == "recovery"]
    assert any(t["model"] == rcfg.llm.recovery_model and not t["fallback"] for t in traces)
    assert any(t["model"] == "deterministic" and "restart_service ad" in t["input_summary"] for t in traces)
    assert any("Recovery agent: remediated" in x for x in lines) and any("RECOVERY FAILED" in x for x in lines)
    assert Ledger(store).open_entries() == []


def test_menu_only_invalid_action_escalates(monkeypatch, rcfg, store):
    sim = Sim()
    d = Stuck(sim)
    chat = FakeChat(lambda n: verdict("format_disk", "ad"))
    lines = []
    e = run(monkeypatch, rcfg, store, d, sim, chat, lines.append)
    assert e["recovery_agent"]["outcome"] == "escalated" and d.restarted == [] and e["recovery_seconds"] is None
    assert actions(store, e["id"])[0]["action"] == "escalate"
    assert any(x.split("] ", 1)[-1].startswith("Recovery agent: ESCALATED") for x in lines)


@pytest.mark.parametrize("service", ["jaeger", "prometheus", "load-generator", "faultline", "valkey-cart", "kafka", "astronomy-db", "payment", "traffic"])
def test_forbidden_restart_targets_rejected(monkeypatch, rcfg, store, service):
    sim = Sim()
    d = Stuck(sim)
    chat = FakeChat(lambda n: verdict("restart_service", service))
    e = run(monkeypatch, rcfg, store, d, sim, chat)
    assert d.restarted == [] and e["recovery_agent"]["outcome"] == "escalated"
    row = actions(store, e["id"])[0]
    assert row["action"] == "restart_service" and row["service"] == service and row["result"].startswith("rejected by guardrails")


def test_data_store_allowed_only_as_own_target(cfg):
    def ctx(target):
        exp = {"id": "e", "fault": "crash", "target": target, "params": {}}
        return recovery.Ctx(cfg=cfg, store=None, driver=None, exp=exp, graph=GRAPH, thresholds={}, base={}, t_revert=0, stop=None, stop_path=None)

    out = recovery.RecoveryOut(diagnosis="d", action="restart_service", service="valkey-cart", reason="r")
    assert recovery.check_action(ctx("ad"), out, ["cart"])[0] is False
    assert recovery.check_action(ctx("valkey-cart"), out, ["cart"])[0] is True
    assert recovery.check_action(ctx("valkey-cart"), out, ["home"])[0] is False
    out2 = recovery.RecoveryOut(diagnosis="d", action="restart_service", service="jaeger", reason="r")
    assert recovery.check_action(ctx("valkey-cart"), out2, ["cart"])[0] is False


def test_restart_service_requires_service():
    with pytest.raises(ValueError):
        recovery.RecoveryOut(diagnosis="d", action="restart_service", reason="r")


def test_text_limits_and_ascii():
    out = recovery.RecoveryOut(diagnosis="x \u2014 " + "y" * 400, action="Escalate", reason="z" * 300)
    assert len(out.diagnosis) == 300 and len(out.reason) == 200 and out.action == "escalate" and out.diagnosis.isascii()


@pytest.mark.parametrize("cap,expected", [(2, 2), (1, 1)])
def test_max_actions(monkeypatch, rcfg, store, cap, expected):
    rcfg.recovery_agent.max_actions = cap
    sim = Sim()
    d = Stuck(sim)
    d.fixes = ()
    chat = FakeChat(lambda n: verdict("restart_service", "ad"))
    e = run(monkeypatch, rcfg, store, d, sim, chat)
    assert d.restarted == ["ad"] * expected and chat.calls == expected
    assert e["recovery_agent"]["outcome"] == "escalated" and len(actions(store, e["id"])) == expected
    assert e["recovery_seconds"] is None


def test_second_action_after_first_fails(monkeypatch, rcfg, store):
    sim = Sim()
    d = Stuck(sim)
    d.fixes = ("cart",)
    chat = FakeChat(lambda n: verdict("restart_service", "ad") if n == 1 else verdict("restart_service", "cart"))
    e = run(monkeypatch, rcfg, store, d, sim, chat)
    assert d.restarted == ["ad", "cart"] and e["recovery_agent"]["outcome"] == "remediated"
    assert [r["service"] for r in actions(store, e["id"])] == ["ad", "cart"]
    assert "Already done: restart_service ad" in chat.last_user


@pytest.mark.parametrize("kind", ["timeout", "none", "invalid"])
def test_llm_failure_escalates_and_does_nothing(monkeypatch, rcfg, store, kind):
    sim = Sim()
    d = Stuck(sim)

    class Boom(FakeChat):
        def invoke(self, msgs):
            raise TimeoutError("slow")

    chat = {"timeout": Boom(lambda n: ""), "none": None, "invalid": FakeChat(lambda n: "not json")}[kind]
    lines = []
    e = run(monkeypatch, rcfg, store, d, sim, chat, lines.append)
    assert d.restarted == [] and e["recovery_agent"]["outcome"] == "escalated" and e["recovery_seconds"] is None
    assert actions(store, e["id"])[0]["action"] == "escalate" and "no diagnosis" in actions(store, e["id"])[0]["result"]
    assert any("Operator:" in x and "faultline stop" in x for x in lines)


def test_operator_stop_does_nothing(monkeypatch, rcfg, store):
    sim = Sim()
    d = SimDriver(sim)
    stop = asyncio.Event()
    d.on_apply = lambda en: stop.set()
    chat = FakeChat(lambda n: verdict("restart_service", "ad"))
    e = run(monkeypatch, rcfg, store, d, sim, chat, stop=stop)
    assert e["recovery_measured"] == 0 and e["recovery_agent"]["outcome"] == "not needed" and e["recovery_agent"]["initial"] == "not measured"
    assert chat.calls == 0 and actions(store, e["id"]) == []


def test_recovery_agent_disabled_does_nothing(monkeypatch, rcfg, store):
    rcfg.recovery_agent.enabled = False
    sim = Sim()
    d = Stuck(sim)
    chat = FakeChat(lambda n: verdict("restart_service", "ad"))
    e = run(monkeypatch, rcfg, store, d, sim, chat)
    assert chat.calls == 0 and d.restarted == [] and e["recovery_agent"]["outcome"] == "escalated"


def test_kill_switch_action_reverts_and_leaves_sentinel(monkeypatch, rcfg, store):
    sim = Sim()
    d = Stuck(sim)
    chat = FakeChat(lambda n: verdict("kill_switch", None, "fault may be active", "stop everything"))
    e = run(monkeypatch, rcfg, store, d, sim, chat)
    try:
        assert e["recovery_agent"]["outcome"] == "escalated" and exp_mod.stop_path().exists()
        assert Ledger(store).open_entries() == [] and d.restarted == []
    finally:
        exp_mod.stop_path().unlink(missing_ok=True)


@pytest.mark.parametrize("action,expect_restart", [("restart_target", ["ad"]), ("reapply_revert", [])])
def test_target_and_reapply_actions(monkeypatch, rcfg, store, action, expect_restart):
    sim = Sim()
    d = Stuck(sim)
    chat = FakeChat(lambda n: verdict(action))
    e = run(monkeypatch, rcfg, store, d, sim, chat)
    assert d.restarted[: len(expect_restart)] == expect_restart
    assert actions(store, e["id"])[0]["action"] == action
    if action == "reapply_revert":
        assert actions(store, e["id"])[0]["result"].startswith("reapplied: #")


def test_analyzer_uses_final_recovery_and_keeps_initial_failure(cfg):
    exp = {
        "id": "e1", "fault": "crash", "target": "ad", "params": {}, "state": "COMPLETED", "injected_at": 1.0, "reverted_at": 5.0,
        "recovery_seconds": 140.0, "recovery_measured": 1, "impact": None, "abort_reason": None, "abort_source": None,
        "recovery_agent": {"outcome": "remediated", "initial": "failed", "actions": [{"action": "restart_service", "service": "ad"}], "note": "back"},
    }
    a = analyzer.analyze_experiment(cfg, exp, None, GRAPH, None)
    assert a["recovery_s"] == 140.0 and a["initial_recovery_failed"] is True and a["recovery_agent"] == "remediated"
    assert a["never_recovered"] is False and a["recovery_norm"] == 1.0
    assert report.fmt_rec(a) == "140.0s (after agent)"
    exp["recovery_seconds"] = None
    exp["recovery_agent"]["outcome"] = "escalated"
    b = analyzer.analyze_experiment(cfg, exp, None, GRAPH, None)
    assert b["never_recovered"] is True and b["initial_recovery_failed"] is True and report.fmt_rec(b) == "never (agent escalated)"


def test_describe_lines():
    assert recovery.describe({"outcome": "not needed", "initial": "recovered", "actions": []}) == "Recovery agent: recovered, no action"
    s = recovery.describe({"outcome": "escalated", "initial": "failed", "actions": [], "note": "unclear"})
    assert s == "Recovery agent: escalated to the operator (unclear)"
