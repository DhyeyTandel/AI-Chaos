import pytest

from faultline.faults import ValidationError, validate


@pytest.mark.parametrize(
    "target", ["faultline", "reaper", "prober", "otel-collector", "prometheus", "jaeger", "load-generator", "nope"]
)
def test_rejects_non_app_targets(cfg, target):
    for fault in ("crash", "delay"):
        with pytest.raises(ValidationError):
            validate(cfg, fault, target, 10, {})


def test_never_list_beats_config(cfg):
    cfg.safety.allow = list(cfg.safety.allow) + ["reaper", "jaeger"]
    with pytest.raises(ValidationError):
        validate(cfg, "delay", "reaper", 10, {})


def test_frontend_proxy_delay_ok_crash_refused(cfg):
    validate(cfg, "delay", "frontend-proxy", 10, {"delay_ms": 50})
    with pytest.raises(ValidationError):
        validate(cfg, "crash", "frontend-proxy", 10, {})


def test_caps(cfg):
    s = cfg.safety
    with pytest.raises(ValidationError):
        validate(cfg, "crash", "cart", s.max_duration_s + 1, {})
    with pytest.raises(ValidationError):
        validate(cfg, "crash", "cart", 0, {})
    for bad in ({"delay_ms": s.max_delay_ms + 1}, {"delay_ms": 0}, {"jitter_ms": s.max_jitter_ms + 1}, {"loss_pct": s.max_loss_pct + 1}):
        with pytest.raises(ValidationError):
            validate(cfg, "delay", "cart", 10, bad)


def test_unknown_fault(cfg):
    with pytest.raises(ValidationError):
        validate(cfg, "explode", "cart", 10, {})


def test_delay_params_normalized(cfg):
    p = validate(cfg, "delay", "cart", 10, {"delay_ms": 100, "jitter_ms": 5})
    assert p["delay_ms"] == 100 and p["loss_pct"] == 0 and p["ttl_s"] == 10 + cfg.safety.expiry_margin_s
