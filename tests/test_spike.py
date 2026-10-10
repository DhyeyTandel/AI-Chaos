import json
from unittest.mock import MagicMock

import httpx
import pytest

from faultline import analyzer, faults, memory, reaper
from faultline.agents import planner, safety, validator
from faultline.faults import DockerDriver, LocustClient, ValidationError, validate
from faultline.injector import Injector
from faultline.ledger import APPLIED, REVERT_FAILED, REVERTED, Ledger


class FakeLocust:
    def __init__(self, users=5, state="running"):
        self.users, self.state, self.down, self.posts = users, state, False, []
        self.client = LocustClient(base_url="http://locust", transport=httpx.MockTransport(self.handle))

    def handle(self, request):
        if self.down:
            raise httpx.ConnectError("refused", request=request)
        if request.method == "GET" and request.url.path == "/stats/requests":
            return httpx.Response(200, json={"state": self.state, "user_count": self.users})
        if request.method == "POST" and request.url.path == "/swarm":
            form = dict(httpx.QueryParams(request.content.decode()))
            self.posts.append(form)
            self.users = int(form["user_count"])
            return httpx.Response(200, json={"success": True})
        return httpx.Response(404)


@pytest.fixture
def locust():
    return FakeLocust()


@pytest.fixture
def inj(store, cfg, locust):
    driver = DockerDriver(client=MagicMock(), locust=locust.client)
    return Injector(Ledger(store), driver, cfg, retry_delay=0)


def spike(users=100, rate=25):
    return {"users": users, "spawn_rate": rate}


def test_inject_stores_count_and_revert_restores_it(store, inj, locust):
    ledger = Ledger(store)
    i = inj.inject("e1", "spike", faults.TRAFFIC, spike(100), 30)
    e = ledger.get(i)
    assert e["state"] == APPLIED and e["revert"]["users"] == 5 and e["expires_at"] > e["created_at"]
    assert locust.users == 100 and locust.posts[0] == {"user_count": "100", "spawn_rate": "25"}
    assert inj.revert(i) == REVERTED
    assert locust.users == 5


def test_revert_restores_stored_value_when_baseline_changed(store, inj, locust):
    i = inj.inject("e1", "spike", faults.TRAFFIC, spike(100), 30)
    locust.users = 130
    assert inj.revert(i) == REVERTED
    assert locust.users == 5


def test_revert_is_idempotent(store, inj, locust):
    i = inj.inject("e1", "spike", faults.TRAFFIC, spike(100), 30)
    inj.revert(i)
    n = len(locust.posts)
    assert inj.revert(i) == REVERTED
    assert inj.revert_all() == {}
    assert len(locust.posts) == n and locust.users == 5


def test_stored_count_is_in_ledger_before_swarm(store, inj, locust):
    ledger = Ledger(store)
    seen = {}
    real = locust.handle

    def spy(request):
        if request.method == "POST":
            seen["revert"] = ledger.all()[0]["revert"]
            seen["state"] = ledger.all()[0]["state"]
        return real(request)

    inj.driver.locust = LocustClient(base_url="http://locust", transport=httpx.MockTransport(spy))
    inj.inject("e1", "spike", faults.TRAFFIC, spike(100), 30)
    assert seen["state"] == "PENDING" and seen["revert"]["users"] == 5


def test_locust_down_leaves_no_applied_entry(store, inj, locust):
    locust.down = True
    with pytest.raises(Exception, match="locust"):
        inj.inject("e1", "spike", faults.TRAFFIC, spike(100), 30)
    states = [e["state"] for e in Ledger(store).all()]
    assert states == [REVERTED] and not Ledger(store).open_entries()
    assert locust.posts == []


def test_locust_not_running_is_refused(store, inj, locust):
    locust.state = "stopped"
    with pytest.raises(Exception, match="not running"):
        inj.inject("e1", "spike", faults.TRAFFIC, spike(100), 30)
    assert not Ledger(store).open_entries() and locust.posts == []


def test_spike_must_exceed_current_users(store, inj, locust):
    locust.users = 120
    with pytest.raises(Exception, match="does not exceed"):
        inj.inject("e1", "spike", faults.TRAFFIC, spike(100), 30)
    assert not Ledger(store).open_entries() and locust.users == 120


def test_swarm_failure_after_snapshot_still_restores(store, inj, locust):
    real = locust.handle

    def flaky(request):
        if request.method == "POST":
            locust.users = 100
            raise httpx.ReadTimeout("slow", request=request)
        return real(request)

    inj.driver.locust = LocustClient(base_url="http://locust", transport=httpx.MockTransport(flaky))
    with pytest.raises(Exception):
        inj.inject("e1", "spike", faults.TRAFFIC, spike(100), 30)
    assert locust.users == 100
    assert [e["state"] for e in Ledger(store).open_entries()] == [REVERT_FAILED]
    inj.driver.locust = locust.client
    assert list(inj.revert_all().values()) == [REVERTED]
    assert locust.users == 5


def test_revert_failure_keeps_entry_open_then_recovers(store, inj, locust):
    i = inj.inject("e1", "spike", faults.TRAFFIC, spike(100), 30)
    locust.down = True
    assert inj.revert(i, retries=2) == REVERT_FAILED
    locust.down = False
    assert inj.revert_all() == {i: REVERTED}
    assert locust.users == 5


def test_reaper_reverts_expired_spike(store, inj, locust, cfg):
    ledger = Ledger(store)
    i = inj.inject("e1", "spike", faults.TRAFFIC, spike(100), 30)
    ledger.beat("e1")
    assert reaper.tick(ledger, inj, cfg) == {}
    e = ledger.get(i)
    assert reaper.tick(ledger, inj, cfg, now=e["expires_at"] + 1) == {i: REVERTED}
    assert locust.users == 5


def test_reaper_reverts_spike_on_stale_heartbeat(store, inj, locust, cfg):
    ledger = Ledger(store)
    i = inj.inject("e1", "spike", faults.TRAFFIC, spike(100), 30)
    ledger.beat("e1")
    now = ledger.heartbeat_ts() + cfg.reaper.stale_s + 20
    assert reaper.tick(ledger, inj, cfg, now=now) == {i: REVERTED}
    assert locust.users == 5


def test_kill_switch_path_reverts_spike(store, inj, locust):
    inj.inject("e1", "spike", faults.TRAFFIC, spike(100), 60)
    assert list(inj.revert_all().values()) == [REVERTED]
    assert locust.users == 5


def test_validate_defaults_and_caps(cfg):
    p = validate(cfg, "spike", faults.TRAFFIC, 30, {})
    assert p["users"] == 50 and p["spawn_rate"] == 25
    cap = cfg.safety.max_spike_users
    assert cap == 150
    assert validate(cfg, "spike", faults.TRAFFIC, 30, {"users": cap})["users"] == cap
    for bad in ({"users": cap + 1}, {"users": 0}, {"users": "x"}, {"spawn_rate": cap + 1}, {"spawn_rate": 0}):
        with pytest.raises(ValidationError):
            validate(cfg, "spike", faults.TRAFFIC, 30, bad)
    with pytest.raises(ValidationError):
        validate(cfg, "spike", faults.TRAFFIC, cfg.safety.max_duration_s + 1, {})


def test_spike_only_on_traffic_and_traffic_only_spike(cfg):
    for target in ("cart", "load-generator", "faultline", "nope"):
        with pytest.raises(ValidationError):
            validate(cfg, "spike", target, 30, {})
    for fault in ("crash", "delay"):
        for target in (faults.TRAFFIC, "load-generator"):
            with pytest.raises(ValidationError):
                validate(cfg, fault, target, 30, {"delay_ms": 10})


def test_validator_rejects_oversized_candidate(cfg, store):
    cand = planner.Candidate(
        id="spike:traffic:999users", fault="spike", kind="spike", target=faults.TRAFFIC,
        params={"users": 999, "spawn_rate": 25}, duration_s=30, label="spike 999 users",
    )
    ok, reason = validator.validate_candidate(cfg, store, cand)
    assert not ok and "users" in reason


GRAPH = {
    "nodes": {"frontend": {"stateful": False, "kind": "app"}, "cart": {"stateful": False, "kind": "app"}},
    "edges": [{"source": "frontend", "target": "cart"}],
    "journeys": {"home": {"services": ["frontend"]}, "cart": {"services": ["frontend", "cart"]}},
}


def test_planner_proposes_spike_candidates(cfg, store):
    cands = {c.id: c for c in planner.generate_candidates(cfg, GRAPH)}
    c = cands["spike:traffic:50users"]
    assert c.fault == "spike" and c.target == faults.TRAFFIC and c.tier == 2 and c.params == {"users": 50, "spawn_rate": 25}
    assert c.duration_s == 30 and "spike:traffic:100users" in cands
    assert validator.validate_candidate(cfg, store, c)[0]


def test_spike_tier_is_configurable_and_overridable(cfg):
    cfg.planner.spike_tier = 3
    c = {c.id: c for c in planner.generate_candidates(cfg, GRAPH)}["spike:traffic:50users"]
    assert c.tier == 3
    cfg.planner.tiers.overrides.__dict__["spike:traffic:50users"] = 1
    assert {c.id: c for c in planner.generate_candidates(cfg, GRAPH)}["spike:traffic:50users"].tier == 1


def test_spike_can_be_disabled(cfg):
    cfg.planner.spike_users = []
    assert not [c for c in planner.generate_candidates(cfg, GRAPH) if c.fault == "spike"]


def test_spike_scores_against_all_journeys(cfg):
    cands = [c for c in planner.generate_candidates(cfg, GRAPH) if c.fault == "spike"]
    s = planner.score_candidates(cfg, GRAPH, [], cands)[0]
    assert s.components["journeys_affected"] == 1.0 and s.components["severity"] == cfg.planner.severity.spike
    assert planner.blast_radius(GRAPH, faults.TRAFFIC) == ["journey:cart", "journey:home"]
    assert "traffic surge" in planner.template_hypothesis(GRAPH, cands[0])


def test_kind_label_memory_keyed_by_users():
    assert analyzer.fault_kind("spike", {"users": 100}) == "spike"
    assert analyzer.fault_label("spike", {"users": 100, "spawn_rate": 25}) == "spike 100 users"
    assert memory.level("spike", {"users": 100}) == 100.0


def test_memory_excludes_spike_at_or_above_aborted_users(cfg, store):
    store.conn.execute(
        "INSERT INTO experiments (id, fault, target, params, state, created_at, abort_source, abort_reason) VALUES (?,?,?,?,?,?,?,?)",
        ("exp-1", "spike", faults.TRAFFIC, json.dumps(spike(100)), "ABORTED", __import__("time").time(), "watchdog", "watchdog breach"),
    )
    store.conn.commit()
    limits = memory.known_limits(cfg, store)
    assert limits[0]["kind"] == "spike" and limits[0]["intensity"] == 100 and limits[0]["label"] == "100 users"
    kept, out = memory.exclusions(planner.generate_candidates(cfg, GRAPH), limits)
    ids = {c.id for c in kept}
    assert "spike:traffic:50users" in ids and "spike:traffic:100users" not in ids
    assert out[0]["candidate"] == "spike:traffic:100users"


def test_analysis_treats_spike_as_hitting_every_journey(cfg):
    assert analyzer.mapped_journeys(GRAPH, faults.TRAFFIC) == {"home", "cart"}
    exp = {
        "id": "exp-1", "fault": "spike", "target": faults.TRAFFIC, "params": spike(100), "state": "COMPLETED", "injected_at": 1.0,
        "reverted_at": 2.0, "recovery_seconds": 7.0, "abort_reason": None,
        "impact": {
            "home": {"count": 10, "error_rate": 0.0, "p95_ms": 900.0, "base_error_rate": 0.0, "base_p95_ms": 50.0},
            "cart": {"count": 10, "error_rate": 0.0, "p95_ms": 60.0, "base_error_rate": 0.0, "base_p95_ms": 50.0},
        },
    }
    a = analyzer.analyze_experiment(cfg, exp, None, GRAPH, None)
    assert a["kind"] == "spike" and a["label"] == "spike 100 users"
    assert a["affected_journeys"] == ["home"] and a["hidden_journeys"] == []
    assert a["verdict"] in ("PARTIAL", "CONFIRMED")


def test_review_may_only_lower_users(cfg):
    p, d = safety.apply_reduction(cfg, "spike", faults.TRAFFIC, 30, validate(cfg, "spike", faults.TRAFFIC, 30, spike(100)), {"users": 60})
    assert p["users"] == 60 and d == 30
    base = validate(cfg, "spike", faults.TRAFFIC, 30, spike(100))
    with pytest.raises(safety.Rejected):
        safety.apply_reduction(cfg, "spike", faults.TRAFFIC, 30, base, {"users": 120})
    with pytest.raises(safety.Rejected):
        safety.apply_reduction(cfg, "spike", faults.TRAFFIC, 30, base, {"spawn_rate": 10})
