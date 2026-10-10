import math

from faultline import baseline as bl


def rows(lat, ok=None):
    ok = ok or [True] * len(lat)
    return [{"ts_wall": float(i), "latency_ms": l, "ok": int(o), "journey": "home"} for i, (l, o) in enumerate(zip(lat, ok))]


def test_summary(cfg):
    s = bl.journey_summary(rows([100, 200, 300, 400]))
    assert s["count"] == 4 and s["success_rate"] == 1.0 and s["error_rate"] == 0.0
    assert s["mean_ms"] == 250 and s["p50_ms"] == 200 and s["p95_ms"] == 400
    assert math.isclose(s["stddev_ms"], math.sqrt(12500))


def test_thresholds_floors_and_scaling(cfg):
    low = bl.thresholds_for(bl.journey_summary(rows([50] * 20)), cfg)
    assert low["error_rate_max"] == cfg.thresholds.error_floor
    assert low["p95_max_ms"] == cfg.thresholds.p95_floor_ms
    slow = bl.thresholds_for(bl.journey_summary(rows([1000] * 20)), cfg)
    assert slow["p95_max_ms"] == 1000 * cfg.thresholds.p95_multiplier


def test_error_threshold_grows_with_baseline_errors(cfg):
    ok = [True] * 10 + [False] * 10
    t = bl.thresholds_for(bl.journey_summary(rows([10] * 20, ok)), cfg)
    sigma = math.sqrt(0.5 * 0.5 / cfg.watchdog.window_s)
    assert math.isclose(t["error_rate_max"], 0.5 + cfg.thresholds.k_sigma * sigma)


def test_save_and_latest(store, cfg):
    assert bl.latest(store) is None
    data = bl.compute({"home": rows([100] * 5)}, cfg)
    bl.save(store, data, {"seconds": 5})
    data2 = bl.compute({"home": rows([200] * 5)}, cfg)
    bl.save(store, data2, {"seconds": 6})
    b = bl.latest(store)
    assert b["params"]["seconds"] == 6 and b["journeys"]["home"]["p95_ms"] == 200


def test_toml_matches_defaults():
    from pathlib import Path

    from faultline.config import DEFAULTS, load_config

    root = Path(__file__).resolve().parents[1] / "faultline.toml"
    c = load_config(path=root)
    d = load_config(path="/nonexistent")
    assert c == d and DEFAULTS["safety"]["allow"] == list(c.safety.allow)
