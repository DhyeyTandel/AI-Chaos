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
    "llm": {
        "planner_model": "openai/gpt-oss-120b",
        "analyst_model": "openai/gpt-oss-120b",
        "fixer_model": "openai/gpt-oss-120b",
        "safety_model": "openai/gpt-oss-20b",
        "rate_per_min": 25,
        "timeout_s": 20,
        "tpm_per_model": 8000,
        "daily_token_budget": 180000,
        "cache_ttl_s": 21600,
        "reasoning_effort": {"planner": "low", "analyst": "medium", "fixer": "medium", "safety": "low"},
        "max_tokens": {"planner": 1500, "analyst": 3000, "fixer": 3000, "safety": 800},
    },
    "planner": {
        "top_k": 8,
        "max_attempts": 3,
        "max_per_target": 2,
        "duration_s": 20,
        "crash_duration_s": 20,
        "delay_ms": [50, 300],
        "loss_pct": [10],
        "exclude": [],
        "history": 10,
        "weights": {"fan_in": 1.0, "journeys": 2.0, "stateful": 1.0, "untested": 1.5, "recent": 1.5, "severity": 0.5},
        "severity": {"crash": 1.0, "delay300": 0.6, "loss": 0.5, "delay50": 0.2},
        "tiers": {"light_delay_ms": 50, "critical": ["flagd"], "overrides": {"delay:frontend:50ms": 2, "delay:product-catalog:50ms": 2}},
    },
    "run": {"climb_after": 2, "cooldown_s": 10, "settle_wait_s": 60},
    "score": {
        "impact_weight": 0.5,
        "breadth_weight": 0.3,
        "recovery_weight": 0.2,
        "recovery_cap_s": 60,
        "err_full": 0.5,
        "p95_full_ratio": 10.0,
        "p95_base_floor_ms": 100,
        "affected_margin": 0.2,
        "hidden_min_impact": 0.3,
        "weak_min_penalty": 10,
        "max_weaknesses": 5,
    },
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
