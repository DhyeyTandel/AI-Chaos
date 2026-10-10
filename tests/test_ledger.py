import time

import pytest

from faultline import faults, reaper
from faultline.injector import Injector
from faultline.ledger import APPLIED, PENDING, REVERT_FAILED, REVERTED, Ledger


def make(store, driver, cfg):
    return Ledger(store), Injector(Ledger(store), driver, cfg, retry_delay=0)


def test_pending_written_before_apply(store, driver, cfg):
    ledger = Ledger(store)
    seen = {}

    def apply(entry):
        seen["state"] = ledger.get(entry["id"])["state"]
        return entry["revert"]

    driver.apply = apply
    inj = Injector(ledger, driver, cfg, retry_delay=0)
    i = inj.inject("e1", "crash", "cart", {}, 10)
    assert seen["state"] == PENDING
    e = ledger.get(i)
    assert e["state"] == APPLIED and e["applied_at"] and e["expires_at"] > e["created_at"]


def test_revert_transitions_and_idempotence(store, driver, cfg):
    ledger, inj = make(store, driver, cfg)
    i = inj.inject("e1", "delay", "cart", {"delay_ms": 100}, 10)
    assert inj.revert(i) == REVERTED
    n = driver.reverts
    assert inj.revert(i) == REVERTED
    assert driver.reverts == n
    assert inj.revert_all() == {}
    assert ledger.get(i)["reverted_at"]


def test_revert_failure_then_recovery(store, driver, cfg):
    ledger, inj = make(store, driver, cfg)
    i = inj.inject("e1", "crash", "cart", {}, 10)
    driver.fail_revert = 3
    assert inj.revert(i, retries=3) == REVERT_FAILED
    assert ledger.get(i)["state"] == REVERT_FAILED and "boom" in ledger.get(i)["error"]
    assert inj.revert_all() == {i: REVERTED}


def test_claim_blocks_concurrent_reverter(store, driver, cfg):
    ledger, inj = make(store, driver, cfg)
    i = inj.inject("e1", "crash", "cart", {}, 10)
    assert ledger.claim(i)
    assert inj.revert(i) is None
    assert inj.revert_all(wait_s=0.2) == {}
    assert ledger.get(i)["state"] == APPLIED


def test_inject_refused_while_faults_open(store, driver, cfg):
    ledger, inj = make(store, driver, cfg)
    inj.inject("e1", "crash", "cart", {}, 10)
    with pytest.raises(faults.ValidationError):
        inj.inject("e2", "crash", "ad", {}, 10)


def test_failed_apply_is_reverted(store, driver, cfg):
    ledger, inj = make(store, driver, cfg)
    driver.apply = lambda e: (_ for _ in ()).throw(RuntimeError("no docker"))
    with pytest.raises(RuntimeError):
        inj.inject("e1", "crash", "cart", {}, 10)
    assert ledger.all()[0]["state"] == REVERTED


def test_orphan_sweep(store, driver, cfg):
    ledger, inj = make(store, driver, cfg)
    driver.owned = [{"name": "faultline-netem-99", "ledger_id": 99, "target": "cart"}]
    assert inj.sweep_orphans() == ["faultline-netem-99"]
    assert driver.owned == []


def test_reaper_reverts_expired_only(store, driver, cfg):
    ledger, inj = make(store, driver, cfg)
    i = inj.inject("e1", "crash", "cart", {}, 10)
    ledger.beat("e1")
    assert reaper.tick(ledger, inj, cfg) == {}
    assert ledger.get(i)["state"] == APPLIED
    assert reaper.tick(ledger, inj, cfg, now=time.time() + 100)[i] == REVERTED


def test_reaper_reverts_on_stale_heartbeat(store, driver, cfg):
    ledger, inj = make(store, driver, cfg)
    i = inj.inject("e1", "crash", "cart", {}, 100)
    ledger.beat("e1")
    now = time.time()
    assert reaper.tick(ledger, inj, cfg, now=now + cfg.reaper.stale_s - 2) == {}
    assert reaper.tick(ledger, inj, cfg, now=now + cfg.reaper.stale_s + 1) == {i: REVERTED}


def test_reaper_startup_reconcile_without_heartbeat(store, driver, cfg):
    ledger, inj = make(store, driver, cfg)
    i = ledger.create("old", "crash", "cart", {}, faults.initial_revert("crash", "cart"), time.time() + 1000)
    ledger.mark_applied(i)
    assert reaper.tick(ledger, inj, cfg, now=time.time() + 11) == {i: REVERTED}
