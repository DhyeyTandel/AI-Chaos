import json
import time
from unittest.mock import MagicMock

import httpx
import pytest

from faultline import experiment as exp_mod
from faultline import faults
from faultline.agents import injection
from faultline.faults import DockerDriver
from faultline.ledger import APPLIED, REVERTED, Ledger
from conftest import FakeDriver
from test_experiment import Sim, SimDriver, add_other, ecfg, go  # noqa: F401
from test_planner import fresh_limiter  # noqa: F401
from test_spike import FakeLocust


def spec(cfg, fault="crash", target="ad", params=None, duration=20):
    return injection.prepare(cfg, fault, target, params or {}, duration)


def combo_spec(cfg):
    parts = [
        {"fault": "delay", "target": "quote", "params": {"delay_ms": 50}},
        {"fault": "spike", "target": "traffic", "params": {"users": 50, "spawn_rate": 25}},
    ]
    return injection.prepare(cfg, faults.COMBO, "x", {"parts": parts}, 20)


@pytest.fixture
def fast(cfg):
    cfg.injection.verify_timeout_s = 0
    cfg.injection.spike_timeout_s = 0
    return cfg


def test_prepare_orders_combo_parts(cfg):
    s = combo_spec(cfg)
    assert [p["kind"] for p in s["parts"]] == ["spike", "delay"]
    assert [p["label"] for p in s["parts"]] == ["spike 50 users", "delay quote 50 ms"]


@pytest.mark.parametrize("fault,target,params", [
    ("crash", "ad", {}), ("delay", "quote", {"delay_ms": 50}), ("delay", "quote", {"delay_ms": 20, "loss_pct": 10}),
    ("spike", "traffic", {"users": 50}),
])
def test_verified_single_part(store, driver, fast, fault, target, params):
    rec = injection.inject(store.path, driver, fast, "e1", spec(fast, fault, target, params), sleep=lambda s: None)
    assert rec["verified"] and len(rec["parts"]) == 1 and rec["parts"][0]["verified"]
    assert [e["state"] for e in Ledger(store).all()] == [APPLIED]
    row = [t for t in store.traces(10) if t["agent"] == "injection"][0]
    assert row["model"] == "deterministic" and row["fallback"] == 0 and row["error"] is None
    assert json.loads(row["output_json"])["verified"] is True and row["latency_ms"] is not None
    assert injection.describe(rec).startswith("Injection agent: verified ")


@pytest.mark.parametrize("kind,fault,target,params", [
    ("crash", "crash", "ad", {}), ("delay", "delay", "quote", {"delay_ms": 50}), ("spike", "spike", "traffic", {"users": 50}),
])
def test_unverified_part_reverts_own_entries_only(store, fast, kind, fault, target, params):
    other = []

    class D(FakeDriver):
        def apply(self, entry):
            other.append(add_other(store, "other"))
            return super().apply(entry)

    d = D()
    d.unverified = {kind}
    with pytest.raises(injection.NotVerified) as ei:
        injection.inject(store.path, d, fast, "e1", spec(fast, fault, target, params), sleep=lambda s: None)
    assert "not in effect" in str(ei.value) and ei.value.record["verified"] is False
    ledger = Ledger(store)
    assert [e["state"] for e in ledger.all() if e["experiment_id"] == "e1"] == [REVERTED]
    assert ledger.get(other[0])["state"] == APPLIED
    row = [t for t in store.traces(10) if t["agent"] == "injection"][0]
    assert row["fallback"] == 0 and row["error"] and json.loads(row["output_json"])["verified"] is False


def test_combo_verifies_every_part_in_apply_order(store, driver, fast):
    rec = injection.inject(store.path, driver, fast, "e1", combo_spec(fast), sleep=lambda s: None)
    assert driver.checked == ["spike", "delay"] and rec["verified"]
    assert [c["part"] for c in rec["parts"]] == ["spike 50 users", "delay quote 50 ms"]
    assert injection.describe(rec) == "Injection agent: verified spike 50 users, delay quote 50 ms"


def test_combo_one_unverified_part_reverts_all_parts(store, driver, fast):
    driver.unverified = {"delay"}
    with pytest.raises(injection.NotVerified) as ei:
        injection.inject(store.path, driver, fast, "e1", combo_spec(fast), sleep=lambda s: None)
    assert "delay quote 50 ms" in str(ei.value) and "spike" not in str(ei.value)
    assert [e["state"] for e in Ledger(store).all()] == [REVERTED, REVERTED]
    assert "NOT verified delay quote 50 ms" in injection.describe(ei.value.record)


def test_verification_polls_until_in_effect(store, cfg):
    calls = []

    class Slow(FakeDriver):
        def check_applied(self, entry, spike_fraction=0.8):
            calls.append(1)
            return len(calls) >= 3, "ramping"

    clock = {"t": 0.0}
    sleeps = []
    ledger = Ledger(store)
    i = ledger.create("e1", "spike", "traffic", {"users": 50, "spawn_rate": 25}, {}, time.time() + 100)
    out = injection.verify(cfg, Slow(), [ledger.get(i)], sleeps.append, lambda: clock["t"])
    assert out[0]["verified"] and len(calls) == 3 and sleeps == [cfg.injection.poll_s] * 2


def test_apply_failure_is_traced_and_raised(store, fast):
    class Boom(FakeDriver):
        def apply(self, entry):
            raise RuntimeError("no docker")

    with pytest.raises(RuntimeError):
        injection.inject(store.path, Boom(), fast, "e1", spec(fast), sleep=lambda s: None)
    row = [t for t in store.traces(10) if t["agent"] == "injection"][0]
    assert row["error"].startswith("apply failed") and [e["state"] for e in Ledger(store).all()] == [REVERTED]


# experiment level

def test_experiment_records_verified_injection(monkeypatch, ecfg, store):
    sim = Sim()
    lines = []
    e = go(monkeypatch, ecfg, store, SimDriver(sim), sim, duration=1, log=lines.append)
    assert e["state"] == "COMPLETED" and e["injection"]["verified"] is True
    assert any("Injection agent: verified crash ad" in x for x in lines)
    assert any(t["agent"] == "injection" and t["model"] == "deterministic" for t in store.traces(20))


def test_experiment_aborts_when_injection_not_verified(monkeypatch, ecfg, store):
    sim = Sim()
    d = SimDriver(sim)
    d.unverified = {"crash"}
    e = go(monkeypatch, ecfg, store, d, sim, duration=2)
    assert e["state"] == "ABORTED" and e["abort_source"] == "inject"
    assert e["abort_reason"].startswith("injection not verified: ")
    assert e["injection"]["verified"] is False and e["injected_at"] is None
    assert [x["state"] for x in Ledger(store).all()] == [REVERTED]
    assert e["recovery_agent"] is None


# docker driver checks

def entry(kind, target, params, revert=None, id_=7):
    return {"id": id_, "kind": kind, "target": target, "params": params, "revert": revert or {}, "expires_at": time.time() + 60}


def test_docker_check_spike_fraction():
    loc = FakeLocust(users=5)
    d = DockerDriver(client=MagicMock(), locust=loc.client)
    e = entry("spike", "traffic", {"users": 100, "spawn_rate": 25})
    assert d.check_applied(e, 0.8)[0] is False
    loc.users = 79
    assert d.check_applied(e, 0.8)[0] is False
    loc.users = 80
    assert d.check_applied(e, 0.8)[0] is True


def test_docker_check_crash():
    client = MagicMock()
    c = MagicMock(status="running")
    client.containers.get.return_value = c
    d = DockerDriver(client=client, locust=FakeLocust().client)
    e = entry("crash", "ad", {})
    assert d.check_applied(e)[0] is False
    c.status = "exited"
    assert d.check_applied(e)[0] is True


def test_docker_check_netem_needs_sidecar_and_qdisc():
    client = MagicMock()
    d = DockerDriver(client=client, locust=FakeLocust().client)
    e = entry("delay", "quote", {"delay_ms": 50})
    client.containers.list.return_value = []
    assert d.check_applied(e) == (False, "netem sidecar not running")
    client.containers.list.return_value = [MagicMock(status="running")]
    client.containers.get.return_value = MagicMock(status="running")
    client.containers.run.return_value = b"qdisc netem 8001: root refcnt 2 limit 1000 delay 50ms"
    assert d.check_applied(e)[0] is True
    client.containers.run.return_value = b"qdisc noqueue 0: root refcnt 2"
    assert d.check_applied(e)[0] is False
