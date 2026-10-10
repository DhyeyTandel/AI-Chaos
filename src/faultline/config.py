import copy
import os
import tomllib
from pathlib import Path
from types import SimpleNamespace

DEFAULTS = {
    "baseline": {"seconds": 60, "warmup": 10},
    "thresholds": {"error_floor": 0.2, "k_sigma": 3.0, "p95_floor_ms": 800, "p95_multiplier": 3.0},
    "watchdog": {
        "interval_s": 1.0,
        "window_s": 10,
        "min_samples": 5,
        "breach_ticks": 2,
        "blind_s": 5,
        "probe_timeout_s": 3.0,
    },
    "preflight": {"probe_s": 8},
    "recovery": {"window_s": 10, "min_samples": 5, "cap_s": 120},
    "safety": {
        "max_duration_s": 120,
        "max_delay_ms": 2000,
        "max_jitter_ms": 500,
        "max_loss_pct": 30,
        "allow": [
            "accounting", "ad", "astronomy-db", "cart", "checkout", "currency", "email",
            "flagd", "fraud-detection", "frontend", "frontend-proxy", "image-provider",
            "kafka", "payment", "product-catalog", "quote", "recommendation", "shipping",
            "valkey-cart",
        ],
        "crash_exclude": ["frontend-proxy"],
        "expiry_margin_s": 5,
    },
    "reaper": {"interval_s": 2.0, "stale_s": 10, "heartbeat_s": 1.0},
    "map": {"lookback_min": 60},
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _ns(d):
    return SimpleNamespace(**{k: _ns(v) if isinstance(v, dict) else v for k, v in d.items()})


def config_path() -> Path:
    return Path(os.environ.get("FAULTLINE_CONFIG", "/app/faultline.toml"))


def load_config(path=None, overrides: dict | None = None) -> SimpleNamespace:
    data = copy.deepcopy(DEFAULTS)
    p = Path(path) if path else config_path()
    if p.is_file():
        data = _merge(data, tomllib.loads(p.read_text()))
    if overrides:
        data = _merge(data, overrides)
    return _ns(data)
