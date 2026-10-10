import pytest

from faultline import watchdog as wd

TH = {j: {"error_rate_max": 0.2, "p95_max_ms": 800} for j in ("home", "cart")}


def mk(j, t, ok=True, lat=100):
    return {"journey": j, "ts_wall": t - lat / 1000, "latency_ms": lat, "ok": int(ok)}


def stream(start, end, ok=True, lat=100, journeys=("home", "cart")):
    return [mk(j, float(t), ok, lat) for t in range(start, end + 1) for j in journeys]


def test_healthy(cfg):
    d = wd.evaluate(stream(90, 100), TH, 100.0, 90.0, cfg)
    assert d.status == wd.OK


def test_error_breach(cfg):
    rows = stream(90, 100) + stream(101, 108, ok=False)
    d = wd.evaluate(rows, TH, 108.0, 90.0, cfg)
    assert d.status == wd.BREACH and "error rate" in d.reason


def test_latency_breach(cfg):
    d = wd.evaluate(stream(90, 100, lat=2000), TH, 100.0, 90.0, cfg)
    assert d.status == wd.BREACH and "p95" in d.reason


def test_blind(cfg):
    d = wd.evaluate(stream(90, 100), TH, 106.5, 90.0, cfg)
    assert d.status == wd.BLIND
    assert wd.evaluate([], TH, 96.0, 90.0, cfg).status == wd.BLIND
    assert wd.evaluate([], TH, 93.0, 90.0, cfg).status == wd.OK


def test_min_samples_prevents_judging(cfg):
    rows = [mk("home", 100.0, ok=False), mk("home", 99.5, ok=False)]
    assert wd.evaluate(rows, TH, 100.0, 90.0, cfg).status == wd.OK


def test_watchdog_requires_consecutive_breaches(cfg):
    w = wd.Watchdog(TH, cfg, 90.0)
    bad = stream(90, 100, ok=False)
    assert w.check(bad, 100.0).status == wd.OK
    assert w.check(bad, 101.0).status == wd.BREACH


def test_watchdog_streak_resets(cfg):
    w = wd.Watchdog(TH, cfg, 90.0)
    assert w.check(stream(90, 100, ok=False), 100.0).status == wd.OK
    assert w.check(stream(100, 110), 110.0).status == wd.OK
    assert w.check(stream(90, 100, ok=False), 100.0).status == wd.OK


def test_blind_is_immediate(cfg):
    w = wd.Watchdog(TH, cfg, 90.0)
    assert w.check([], 100.0).status == wd.BLIND


def test_recovered(cfg):
    rows = stream(90, 100, ok=False) + stream(101, 104)
    assert not wd.recovered(rows, TH, 104.0, 100.5, cfg)
    rows += stream(105, 112)
    assert wd.recovered(rows, TH, 112.0, 100.5, cfg)
    assert not wd.recovered(stream(101, 106, ok=False), TH, 106.0, 100.5, cfg)


def test_impact():
    base = {"journeys": {"home": {"error_rate": 0.0, "p95_ms": 100}}}
    rows = [mk("home", 100.0, ok=False, lat=500), mk("home", 101.0, lat=300), mk("home", 50.0)]
    r = wd.impact(rows, base, 99.0, 102.0)["home"]
    assert r["count"] == 2 and r["error_rate"] == 0.5 and r["p95_ms"] == 500 and r["base_p95_ms"] == 100
