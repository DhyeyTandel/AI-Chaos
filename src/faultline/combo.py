from itertools import combinations

from . import analyzer, faults
from .faults import COMBO, ValidationError

APPLY_RANK = {"spike": 0, "delay": 1, "loss": 1, "crash": 2}
PART_KEYS = {
    "delay": ("delay_ms", "jitter_ms", "loss_pct"),
    "loss": ("loss_pct", "jitter_ms"),
    "spike": ("users", "spawn_rate"),
    "crash": (),
}


def kind_of(part: dict) -> str:
    return analyzer.fault_kind(part["fault"], part["params"])


def order(parts: list[dict]) -> list[dict]:
    """Fixed apply order: spike first, then delay or loss, crash last, so the destructive part is never applied before a part that can still fail."""
    return sorted(parts, key=lambda p: (APPLY_RANK[kind_of(p)], p["target"]))


def target_key(parts: list[dict]) -> str:
    return analyzer.COMBO_SEP.join(p["target"] for p in order(parts))


INTENSITY = {"delay": "delay_ms", "loss": "loss_pct", "spike": "users"}


def signature(parts: list[dict]) -> tuple:
    """Identity of a combo for 'already tested': kind, target and intensity of every part."""
    return tuple(sorted((kind_of(p), p["target"], float(p["params"].get(INTENSITY.get(kind_of(p), ""), 0) or 0)) for p in parts))


def limit_key(parts: list[dict]) -> tuple[str, ...]:
    return tuple(sorted(f"{kind_of(p)}:{p['target']}" for p in parts))


def _pair(a: str, b: str) -> str:
    return "+".join(sorted((a, b)))


def validate(cfg, params: dict, duration: float) -> dict:
    c = cfg.combined
    if not c.enabled:
        raise ValidationError("combined failures are disabled ([combined] enabled = false)")
    raw = params.get("parts") if isinstance(params, dict) else None
    if not isinstance(raw, list) or len(raw) < 2:
        raise ValidationError("a combo needs at least 2 parts")
    if len(raw) > c.max_parts:
        raise ValidationError(f"a combo has at most {c.max_parts} parts")
    parts = []
    for i, p in enumerate(raw, 1):
        try:
            fault, target, pp = p["fault"], p["target"], dict(p.get("params") or {})
        except (KeyError, TypeError, AttributeError, ValueError):
            raise ValidationError(f"part {i} needs fault, target and params")
        try:
            v = faults.validate(cfg, fault, target, duration, pp)
        except ValidationError as e:
            raise ValidationError(f"part {i} ({fault} {target}): {e}")
        parts.append({"fault": fault, "target": target, "params": v})
    targets = [p["target"] for p in parts]
    if len(set(targets)) != len(targets):
        raise ValidationError("parts must target different things")
    kinds = [kind_of(p) for p in parts]
    if kinds.count("crash") > 1:
        raise ValidationError("at most one crash per combo")
    for p, k in zip(parts, kinds):
        if k == "crash" and p["target"] in c.crash_deny:
            raise ValidationError(f"target {p['target']!r} may not be crashed inside a combo")
    allowed = {_pair(*x.split("+")) for x in c.allowed_pairs}
    for (ka, _), (kb, _) in combinations(list(zip(kinds, parts)), 2):
        if _pair(ka, kb) not in allowed:
            raise ValidationError(f"{ka}+{kb} is not an allowed combination; allowed: {', '.join(sorted(allowed))}")
    return {
        "duration_s": duration, "parts": order(parts),
        "threshold_factors": {"p95": c.p95_factor, "error": c.error_factor},
    }


def parse(spec: str) -> dict:
    """CLI part syntax: fault:target[:key=value,...]; fault is crash, delay, loss or spike (loss is delay with packet loss)."""
    bits = spec.split(":", 2)
    if len(bits) < 2 or not bits[0].strip() or not bits[1].strip():
        raise ValidationError(f"combo part {spec!r}: expected fault:target[:key=value,...]")
    fault, target = bits[0].strip().lower(), bits[1].strip()
    if fault not in PART_KEYS:
        raise ValidationError(f"combo part {spec!r}: fault must be one of {', '.join(PART_KEYS)}")
    params: dict = {}
    for kv in (bits[2].split(",") if len(bits) == 3 and bits[2].strip() else []):
        k, _, v = kv.partition("=")
        k = k.strip()
        if k not in PART_KEYS[fault]:
            raise ValidationError(f"combo part {spec!r}: {fault} takes {', '.join(PART_KEYS[fault]) or 'no parameters'}, not {k!r}")
        try:
            params[k] = float(v)
        except ValueError:
            raise ValidationError(f"combo part {spec!r}: {k} needs a number")
    if fault == "loss":
        if not params.get("loss_pct"):
            raise ValidationError(f"combo part {spec!r}: loss needs loss_pct=<percent>")
        fault = "delay"
        params.setdefault("delay_ms", 1.0)
    return {"fault": fault, "target": target, "params": params}


def tighten(cfg, thresholds: dict) -> dict:
    c = cfg.combined
    return {
        j: {**t, "error_rate_max": t["error_rate_max"] * c.error_factor, "p95_max_ms": t["p95_max_ms"] * c.p95_factor}
        for j, t in thresholds.items()
    }
