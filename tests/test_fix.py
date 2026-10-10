import copy
import json
import time

import pytest

from faultline import analyzer, fixes as fx, reaper, report
from faultline.agents.fixer import Fix
from faultline.injector import Injector
from faultline.ledger import APPLIED as L_APPLIED, Ledger

MB = fx.MB


class FakeContainer:
    def __init__(self, service, hc):
        self.name = f"{service}-1"
        self.labels = {fx.SERVICE_LABEL: service, fx.PROJECT_LABEL: "ai-chaos"}
        self.status = "running"
        self.attrs = {"HostConfig": hc, "State": {"Status": "running", "Health": {"Status": "healthy"}}, "RestartCount": 0}
        self.updates = []
        self.on_update = None

    def reload(self):
        pass

    def update(self, **kw):
        if self.on_update:
            self.on_update(kw)
        self.updates.append(kw)
        hc = self.attrs["HostConfig"]
        if "restart_policy" in kw:
            hc["RestartPolicy"] = dict(kw["restart_policy"])
        if "mem_limit" in kw:
            hc["Memory"] = kw["mem_limit"]
        if "memswap_limit" in kw:
            hc["MemorySwap"] = kw["memswap_limit"]
        if "cpu_quota" in kw:
            hc["CpuQuota"] = kw["cpu_quota"]


class FakeContainers:
    def __init__(self, cs):
        self.cs = cs

    def list(self, all=False, filters=None):
        want = {x.split("=", 1)[1] for x in filters["label"] if x.startswith(fx.SERVICE_LABEL)}
        return [c for c in self.cs if c.labels[fx.SERVICE_LABEL] in want]


class FakeClient:
    def __init__(self, *cs):
        self.containers = FakeContainers(list(cs))


def hc(mem=64, swap=128, restart="unless-stopped", quota=0):
    return {"RestartPolicy": {"Name": restart, "MaximumRetryCount": 0}, "Memory": mem * MB, "MemorySwap": swap * MB, "CpuQuota": quota, "CpuPeriod": 100000, "NanoCpus": 0}


def fix_of(body, type_="config", svc="checkout"):
    import yaml

    return Fix(weakness_rank=1, fix_title="t", fix_type=type_, steps="s", compose_override=yaml.safe_dump({"services": {svc: body}}))


@pytest.fixture
def fcfg(cfg):
    cfg.fix.settle_s = 0
    cfg.fix.stable_timeout_s = 5
    return cfg


def test_applicable_memory_and_restart(cfg):
    change, why = fx.applicable(cfg, fix_of({"restart": "always", "deploy": {"resources": {"limits": {"memory": "128M"}}}}))
    assert why is None and change["service"] == "checkout"
    assert change["restart"] == {"Name": "always", "MaximumRetryCount": 0} and change["memory_bytes"] == 128 * MB and change["skipped"] == []
    assert fx.describe_change(change) == "checkout: restart always, memory 128 MB"


def test_healthcheck_part_stays_propose_only(cfg):
    change, _ = fx.applicable(cfg, fix_of({"restart": "always", "healthcheck": {"test": ["CMD", "true"]}}))
    assert change["restart"]["Name"] == "always" and change["skipped"] == ["healthcheck"]
    change, why = fx.applicable(cfg, fix_of({"healthcheck": {"test": ["CMD", "true"]}}))
    assert change is None and "propose-only" in why


@pytest.mark.parametrize("fix,part", [
    (fix_of({"restart": "always"}, type_="code"), "code"),
    (fix_of({"restart": "always"}, type_="architecture"), "architecture"),
    (fix_of({"deploy": {"resources": {"limits": {"memory": "2G"}}}}), "max_memory_mb"),
    (fix_of({"deploy": {"resources": {"limits": {"cpus": "4"}}}}), "max_cpus"),
    (fix_of({"restart": "always"}, svc="prometheus"), "allowlisted"),
    (fix_of({"restart": "always"}, svc="load-generator"), "allowlisted"),
    (Fix(weakness_rank=1, fix_title="t", fix_type="config", steps="s"), "no compose override"),
])
def test_non_applicable_fixes_stay_propose_only(cfg, fix, part):
    change, why = fx.applicable(cfg, fix)
    assert change is None and part in why


def test_two_services_are_not_applicable(cfg):
    import yaml

    f = Fix(weakness_rank=1, fix_title="t", fix_type="config", steps="s",
            compose_override=yaml.safe_dump({"services": {"cart": {"restart": "always"}, "checkout": {"restart": "always"}}}))
    assert fx.applicable(cfg, f)[0] is None


def test_applicability_text_labels_propose_only(cfg):
    assert fx.applicability_text(cfg, fix_of({"restart": "always"}, type_="code")).startswith("Propose-only")
    assert fx.applicability_text(cfg, fix_of({"restart": "always"})).startswith("Auto-applicable")


def test_limits_are_only_raised_where_one_exists():
    ch = {"service": "s", "memory_bytes": 32 * MB, "skipped": []}
    with pytest.raises(fx.Refused, match="would lower"):
        fx.live_plan(ch, hc(mem=64))
    with pytest.raises(fx.Refused, match="memory: no existing limit"):
        fx.live_plan({**ch, "memory_bytes": 128 * MB}, {"Memory": 0, "MemorySwap": 0})
    with pytest.raises(fx.Refused, match="already"):
        fx.live_plan({"service": "s", "memory_bytes": 64 * MB, "skipped": []}, hc(mem=64))
    with pytest.raises(fx.Refused, match="cpus: no existing limit"):
        fx.live_plan({"service": "s", "cpus": 1.0, "skipped": []}, hc())
    kw, orig, _ = fx.live_plan({"service": "s", "cpus": 1.0, "skipped": []}, hc(quota=50000))
    assert kw == {"cpu_quota": 100000} and orig == {"cpu": {"CpuQuota": 50000}}


def test_unsupported_limit_part_is_skipped_when_something_else_applies():
    ch = {"service": "s", "restart": {"Name": "always", "MaximumRetryCount": 0}, "cpus": 0.5, "memory_bytes": 32 * MB, "skipped": []}
    kw, orig, lines = fx.live_plan(ch, hc())
    assert kw == {"restart_policy": {"Name": "always", "MaximumRetryCount": 0}} and list(orig) == ["restart"]
    assert any("cpus: no existing limit" in x for x in lines) and any("would lower" in x for x in lines)


def test_swap_scales_with_memory():
    kw, _, _ = fx.live_plan({"service": "s", "memory_bytes": 256 * MB, "skipped": []}, hc(mem=64, swap=128))
    assert kw == {"mem_limit": 256 * MB, "memswap_limit": 512 * MB}


def applier(store, fcfg, c):
    return fx.Applier(store, fcfg, FakeClient(c))


CHANGE = {"service": "checkout", "restart": {"Name": "always", "MaximumRetryCount": 0}, "memory_bytes": 128 * MB, "skipped": []}


def test_write_ahead_before_docker_update(store, fcfg):
    c = FakeContainer("checkout", hc())
    a = applier(store, fcfg, c)
    seen = {}

    def peek(kw):
        row = fx.Fixes(store).all()[0]
        seen.update(state=row["state"], original=row["original"])

    c.on_update = peek
    fid = a.apply(CHANGE, "run-1", 1, ["exp-1"])
    assert seen["state"] == fx.PENDING
    assert seen["original"]["memory"] == {"Memory": 64 * MB, "MemorySwap": 128 * MB}
    assert seen["original"]["restart"] == {"Name": "unless-stopped", "MaximumRetryCount": 0}
    assert fx.Fixes(store).get(fid)["state"] == fx.APPLIED


def test_apply_then_revert_restores_originals_exactly(store, fcfg):
    c = FakeContainer("checkout", hc())
    before = copy.deepcopy(c.attrs["HostConfig"])
    a = applier(store, fcfg, c)
    fid = a.apply(CHANGE, None, None, ["exp-1"])
    now = c.attrs["HostConfig"]
    assert now["Memory"] == 128 * MB and now["MemorySwap"] == 256 * MB and now["RestartPolicy"]["Name"] == "always"
    assert a.revert(fid) == fx.REVERTED
    assert c.attrs["HostConfig"] == before
    assert fx.Fixes(store).get(fid)["state"] == fx.REVERTED
    assert a.revert(fid) == fx.REVERTED and len(c.updates) == 2


def test_failed_apply_restores_and_records(store, fcfg):
    c = FakeContainer("checkout", hc())
    before = copy.deepcopy(c.attrs["HostConfig"])
    real = c.update

    def flaky(**kw):
        if "mem_limit" in kw and kw["mem_limit"] == 128 * MB:
            real(restart_policy=kw["restart_policy"])
            raise RuntimeError("boom")
        real(**kw)

    c.update = flaky
    with pytest.raises(fx.Refused, match="restored"):
        applier(store, fcfg, c).apply(CHANGE, None, None, ["e"])
    assert c.attrs["HostConfig"] == before
    row = fx.Fixes(store).all()[0]
    assert row["state"] == fx.REVERTED and "boom" in row["error"]


def test_failed_revert_is_kept_open(store, fcfg):
    c = FakeContainer("checkout", hc())
    a = applier(store, fcfg, c)
    fid = a.apply(CHANGE, None, None, ["e"])
    c.update = lambda **kw: (_ for _ in ()).throw(RuntimeError("daemon down"))
    assert a.revert(fid) == fx.REVERT_FAILED
    assert [f["id"] for f in a.fixes.open()] == [fid]


def test_apply_refused_with_open_ledger_or_open_fix(store, fcfg):
    c = FakeContainer("checkout", hc())
    a = applier(store, fcfg, c)
    Ledger(store).create("e", "delay", "cart", {}, {}, time.time() + 100)
    with pytest.raises(fx.Refused, match="ledger"):
        a.apply(CHANGE, None, None, ["e"])
    assert not c.updates and not a.fixes.all()
    store.conn.execute("DELETE FROM ledger")
    store.conn.commit()
    a.apply(CHANGE, None, None, ["e"])
    with pytest.raises(fx.Refused, match="unreverted"):
        a.apply({**CHANGE, "restart": {"Name": "no", "MaximumRetryCount": 0}}, None, None, ["e"])


def test_revert_all(store, fcfg):
    c = FakeContainer("checkout", hc())
    before = copy.deepcopy(c.attrs["HostConfig"])
    a = applier(store, fcfg, c)
    fid = a.apply(CHANGE, None, None, ["e"])
    assert a.revert_all() == {fid: fx.REVERTED} and c.attrs["HostConfig"] == before and a.revert_all() == {}


def test_reaper_and_stop_do_not_revert_fixes(store, fcfg, driver):
    c = FakeContainer("checkout", hc())
    a = applier(store, fcfg, c)
    fid = a.apply(CHANGE, None, None, ["e"])
    applied = copy.deepcopy(c.attrs["HostConfig"])
    ledger = Ledger(store)
    lid = ledger.create("e", "crash", "cart", {}, {}, time.time() - 5)
    ledger.mark_applied(lid)
    inj = Injector(ledger, driver, fcfg, retry_delay=0)
    assert reaper.tick(ledger, inj, fcfg, now=time.time() + 1000) == {lid: "REVERTED"}
    assert inj.revert_all() == {}
    assert a.fixes.get(fid)["state"] == fx.APPLIED and c.attrs["HostConfig"] == applied
    assert len(c.updates) == 1


def test_verdict_thresholds(fcfg):
    assert fx.verdict(fcfg, [40.0], [30.0]) == fx.IMPROVED
    assert fx.verdict(fcfg, [40.0], [35.0]) == fx.IMPROVED
    assert fx.verdict(fcfg, [40.0], [36.0]) == fx.NO_CHANGE
    assert fx.verdict(fcfg, [40.0], [44.0]) == fx.NO_CHANGE
    assert fx.verdict(fcfg, [40.0], [45.0]) == fx.WORSE
    assert fx.verdict(fcfg, [40.0, 20.0], [30.0, 20.0]) == fx.IMPROVED
    assert fx.verdict(fcfg, [40.0, 20.0], [30.0]) == fx.INCONCLUSIVE
    fcfg.fix.improve_min_penalty = 20
    assert fx.verdict(fcfg, [40.0], [30.0]) == fx.NO_CHANGE


def exp(eid, pen, **kw):
    return {
        "id": eid, "pen": pen, "state": "COMPLETED", "injected_at": 1.0, "abort_source": None, "abort_reason": None, "fault": "spike",
        "target": "traffic", "params": {"duration_s": 30, "users": 100, "spawn_rate": 25.0}, "impact": {"home": {"p95_ms": pen * 10, "error_rate": 0.0}}, **kw,
    }


@pytest.fixture
def pen(monkeypatch):
    monkeypatch.setattr(fx, "penalty_of", lambda cfg, store, e, graph: e["pen"])


def go(store, fcfg, afters, **kw):
    c = FakeContainer("checkout", hc())
    a = applier(store, fcfg, c)
    before = copy.deepcopy(c.attrs["HostConfig"])
    it = iter(afters)
    f = fx.run_fix(store, fcfg, a, CHANGE, [exp("b1", 40.0)], lambda e: next(it), {}, "run-1", 1, log=lambda m: None, **kw)
    return f, c, before


def test_improved_fix_is_kept(store, fcfg, pen):
    f, c, before = go(store, fcfg, [exp("a1", 10.0)])
    assert f["verdict"] == fx.IMPROVED and f["state"] == fx.APPLIED
    assert f["before_penalty"] == 40.0 and f["after_penalty"] == 10.0 and f["after_experiment_ids"] == ["a1"]
    assert c.attrs["HostConfig"]["Memory"] == 128 * MB
    assert f["details"]["pairs"][0]["journeys"]["home"]["p95_before"] == 400.0


def test_no_change_is_reverted_by_default(store, fcfg, pen):
    f, c, before = go(store, fcfg, [exp("a1", 38.0)])
    assert f["verdict"] == fx.NO_CHANGE and f["state"] == fx.REVERTED and c.attrs["HostConfig"] == before


def test_worse_is_reverted_even_without_keep_if_improved(store, fcfg, pen):
    fcfg.fix.keep_if_improved = False
    f, c, before = go(store, fcfg, [exp("a1", 70.0)])
    assert f["verdict"] == fx.WORSE and f["state"] == fx.REVERTED and c.attrs["HostConfig"] == before


def test_keep_if_improved_false_keeps_no_change(store, fcfg, pen):
    fcfg.fix.keep_if_improved = False
    f, c, before = go(store, fcfg, [exp("a1", 38.0)])
    assert f["verdict"] == fx.NO_CHANGE and f["state"] == fx.APPLIED


def test_reduced_or_vetoed_rerun_is_inconclusive_and_reverted(store, fcfg, pen):
    f, c, before = go(store, fcfg, [exp("a1", 5.0, params={"duration_s": 30, "users": 50, "spawn_rate": 25.0})])
    assert f["verdict"] == fx.INCONCLUSIVE and f["state"] == fx.REVERTED and "changed" in f["details"]["reason"] and f["after_penalty"] is None
    assert c.attrs["HostConfig"] == before
    f, c, before = go(store, fcfg, [exp("a2", 0.0, state="VETOED", injected_at=None)])
    assert f["verdict"] == fx.INCONCLUSIVE and "vetoed" in f["details"]["reason"]


def test_rerun_error_reverts_fix(store, fcfg, pen):
    c = FakeContainer("checkout", hc())
    before = copy.deepcopy(c.attrs["HostConfig"])
    a = applier(store, fcfg, c)

    def boom(e):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        fx.run_fix(store, fcfg, a, CHANGE, [exp("b1", 40.0)], boom, {}, log=lambda m: None)
    assert c.attrs["HostConfig"] == before
    row = a.fixes.all()[0]
    assert row["state"] == fx.REVERTED and row["verdict"] == fx.INCONCLUSIVE


def test_unstable_container_skips_rerun_and_reverts(store, fcfg, pen):
    c = FakeContainer("checkout", hc())
    a = applier(store, fcfg, c)
    before = copy.deepcopy(c.attrs["HostConfig"])
    c.attrs["State"]["Health"]["Status"] = "unhealthy"
    fcfg.fix.stable_timeout_s = 0.05
    calls = []
    f = fx.run_fix(store, fcfg, a, CHANGE, [exp("b1", 40.0)], lambda e: calls.append(e), {}, log=lambda m: None)
    assert not calls and f["verdict"] == fx.INCONCLUSIVE and f["state"] == fx.REVERTED and c.attrs["HostConfig"] == before


def test_aborted_rerun_by_watchdog_counts(store, fcfg, pen):
    f, c, before = go(store, fcfg, [exp("a1", 80.0, state="ABORTED", abort_source="watchdog")])
    assert f["verdict"] == fx.WORSE and f["state"] == fx.REVERTED
    f, c, before = go(store, fcfg, [exp("a2", 80.0, state="ABORTED", abort_source="preflight")])
    assert f["verdict"] == fx.INCONCLUSIVE


def test_after_fix_experiments_are_excluded_from_scores(store, fcfg, pen):
    from faultline import baseline as bl

    th = {"home": {"error_rate_max": 0.2, "p95_max_ms": 800}}
    bl.save(store, {"journeys": {"home": {"error_rate": 0.0, "p95_ms": 50.0, "count": 60, "success_rate": 1.0}}, "thresholds": th}, {})
    imp = json.dumps({"home": {"count": 10, "error_rate": 0.0, "base_error_rate": 0.0, "p95_ms": 900.0, "base_p95_ms": 50.0}})
    for eid, t in (("exp-b", 1000.0), ("exp-a", 2000.0)):
        with store.conn:
            store.conn.execute(
                "INSERT INTO experiments (id, fault, target, params, state, created_at, injected_at, reverted_at, impact) VALUES (?, 'spike', 'traffic', ?, 'COMPLETED', ?, ?, ?, ?)",
                (eid, json.dumps({"users": 100, "spawn_rate": 25}), t, t, t + 30, imp),
            )
    graph = {"nodes": {"frontend": {"kind": "app"}}, "journeys": {"home": {"services": ["frontend"]}}}
    run = {"run_id": "r", "ended_at": 5000.0}
    a = analyzer.analyze(fcfg, store, graph, run, [])
    assert {x["experiment_id"] for x in a["experiments"]} == {"exp-b", "exp-a"}
    c = FakeContainer("checkout", hc())
    ap = applier(store, fcfg, c)
    fid = ap.apply(CHANGE, "r", 1, ["exp-b"])
    ap.fixes.set(fid, after_experiment_ids=["exp-a"], verdict=fx.NO_CHANGE, before_penalty=40.0, after_penalty=38.0, details={"reason": None, "pairs": []})
    a = analyzer.analyze(fcfg, store, graph, run, [])
    assert [x["experiment_id"] for x in a["experiments"]] == ["exp-b"] and list(a["after_fix"]) == ["exp-a"]
    assert a["after_fix"]["exp-a"]["scope"] == "after_fix"
    rows = fx.report_rows(store, a)
    assert rows[0]["id"] == fid and rows[0]["after"][0]["experiment_id"] == "exp-a"
    text = "\n".join(str(b) for b in report.fix_blocks(report.fix_data(store, a)))
    assert "Fix verification" in text and "NO CHANGE" in text and "exp-a" in text
    assert "No fix was applied" in str(report.fix_blocks([]))


def test_cli_flow_confirmation_and_override(store, fcfg, pen, monkeypatch):
    from faultline import cli

    with store.conn:
        store.conn.execute(
            "INSERT INTO experiments (id, fault, target, params, state, created_at, injected_at) VALUES ('b1', 'spike', 'traffic', ?, 'COMPLETED', 1, 1)",
            (json.dumps({"duration_s": 30, "users": 100, "spawn_rate": 25.0}),),
        )
    c = FakeContainer("checkout", hc())
    before = copy.deepcopy(c.attrs["HostConfig"])
    gmap = {"nodes": {"checkout": {}, "frontend": {}}, "journeys": {}}
    ran = []

    def fake_run(st, cfg, fault, target, duration, params, ctx, g, review=False):
        ran.append((fault, target, duration, params["users"], review))
        return exp("a1", 10.0)

    monkeypatch.setattr(cli, "_run_experiment", fake_run)
    monkeypatch.setattr(fx, "penalty_of", lambda cfg, st, e, g: 40.0 if e["id"] == "b1" else e["pen"])
    fixes = [{"rank": 1, "title": "w", "fix": fix_of({"restart": "always"}, type_="code"), "experiment_ids": ["b1"]},
             {"rank": 2, "title": "w2", "fix": fix_of({"deploy": {"resources": {"limits": {"memory": "128M"}}}}), "experiment_ids": ["b1"]}]
    assert cli._fix_flow(fcfg, store, gmap, fixes, None, None, False, None, FakeClient(c)) is False
    assert not ran and c.attrs["HostConfig"] == before and not fx.Fixes(store).all()
    assert cli._fix_flow(fcfg, store, gmap, fixes, None, None, True, None, FakeClient(c)) is True
    assert ran == [("spike", "traffic", 30, 100, True)]
    row = fx.Fixes(store).all()[0]
    assert row["weakness_rank"] == 2 and row["verdict"] == fx.IMPROVED and row["state"] == fx.APPLIED
    assert fx.Applier(store, fcfg, FakeClient(c)).revert_all() == {row["id"]: fx.REVERTED}
    assert c.attrs["HostConfig"] == before
    bad = cli._fix_flow(fcfg, store, gmap, fixes, None, 1, True, None, FakeClient(c))
    assert bad is False
    assert cli._fix_flow(fcfg, store, gmap, fixes, None, 1, True, "services:\n  checkout:\n    restart: always\n", FakeClient(c)) is True
