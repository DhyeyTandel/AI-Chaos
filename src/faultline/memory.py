import json
import math
import time

from . import analyzer, combo

SAFETY_SOURCES = ("watchdog", "safety_agent")
PROMPT_MAX = 20


def level(kind: str, params: dict) -> float:
    """Comparable fault intensity: delay_ms, loss_pct, users, or infinity for a crash."""
    if kind == "crash":
        return math.inf
    key = {"loss": "loss_pct", "spike": "users"}.get(kind, "delay_ms")
    return float((params or {}).get(key, 0) or 0)


def _fmt(kind: str, v: float | None) -> str:
    if kind == "crash" or v is None:
        return "crash"
    return f"{v:g} {'%' if kind == 'loss' else 'users' if kind == 'spike' else 'ms'}"


def enabled(cfg) -> bool:
    return bool(cfg.planner.memory.enabled)


def legacy_source(reason: str | None) -> str | None:
    """Rows written before abort_source existed: the abort reason text names the layer."""
    r = reason or ""
    return "watchdog" if r.startswith("watchdog") else "safety_agent" if r.startswith("safety agent") else None


def known_limits(cfg, store, now: float | None = None) -> list[dict]:
    """Per (target, fault kind), the lowest intensity that the watchdog or safety agent aborted within the lookback."""
    if not enabled(cfg):
        return []
    since = (now or time.time()) - cfg.planner.memory.lookback_days * 86400
    rows = store.conn.execute(
        "SELECT id, fault, target, params, abort_source, abort_reason, created_at FROM experiments WHERE state = 'ABORTED' "
        "AND created_at >= ? ORDER BY created_at, id",
        (since,),
    )
    best: dict[tuple[str, str], dict] = {}
    for r in rows:
        if r["fault"] == analyzer.COMBO:
            continue
        source = r["abort_source"] or legacy_source(r["abort_reason"])
        if source not in SAFETY_SOURCES:
            continue
        params = json.loads(r["params"])
        kind = analyzer.fault_kind(r["fault"], params)
        lv = level(kind, params)
        cur = best.get((r["target"], kind))
        if cur is None or lv < cur["_level"]:
            best[(r["target"], kind)] = {
                "target": r["target"], "kind": kind, "intensity": None if math.isinf(lv) else lv, "label": _fmt(kind, lv),
                "experiment_id": r["id"], "source": source, "at": r["created_at"], "_level": lv,
            }
    return [{k: v for k, v in x.items() if k != "_level"} for _, x in sorted(best.items())]


def _limit_level(lim: dict) -> float:
    return math.inf if lim["intensity"] is None else lim["intensity"]


def exclusions(cands, limits: list[dict]) -> tuple[list, list[dict]]:
    """Drop candidates at or above a known abort intensity for the same target and fault kind."""
    by = {(x["target"], x["kind"]): x for x in limits}
    kept, out = [], []
    for c in cands:
        kind = analyzer.fault_kind(c.fault, c.params)
        lim = by.get((c.target, kind))
        lv = level(kind, c.params)
        if lim is None or lv < _limit_level(lim):
            kept.append(c)
            continue
        if kind == "crash":
            reason = f"crash on {c.target} aborted in {lim['experiment_id']}; crash excluded"
        else:
            reason = (
                f"{kind} on {c.target} aborted at {_fmt(kind, lim['intensity'])} in {lim['experiment_id']}; "
                f"{_fmt(kind, lv)} excluded"
            )
        out.append({"candidate": c.id, "target": c.target, "kind": kind, "reason": reason, "evidence": lim["experiment_id"]})
    return kept, out


def prompt_text(limits: list[dict]) -> str:
    items = []
    for x in limits[:PROMPT_MAX]:
        what = "crash" if x["kind"] == "crash" else f"{x['kind']} >= {x['label'].replace(' ', '')}"
        items.append(f"{x['target']} {what} ({x['experiment_id']})")
    more = f", +{len(limits) - PROMPT_MAX} more" if len(limits) > PROMPT_MAX else ""
    return "; ".join(items) + more


def _sorted(parts: list[dict]) -> list[dict]:
    return sorted(parts, key=lambda p: (combo.kind_of(p), p["target"]))


def _part_levels(parts: list[dict]) -> list[float]:
    return [level(combo.kind_of(p), p["params"]) for p in parts]


def _part_text(parts: list[dict]) -> str:
    return " + ".join(analyzer.part_label(p) for p in parts)


def combo_limits(cfg, store, now: float | None = None) -> list[dict]:
    """Aborted combos (watchdog or safety agent) within the lookback, keyed by the sorted pair of parts. A limit never touches single-fault limits.
    Per key only the aborts no other abort of the same key undercuts on every part are kept."""
    if not enabled(cfg):
        return []
    since = (now or time.time()) - cfg.planner.memory.lookback_days * 86400
    rows = store.conn.execute(
        "SELECT id, params, abort_source, abort_reason, created_at FROM experiments WHERE state = 'ABORTED' AND fault = ? "
        "AND created_at >= ? ORDER BY created_at, id",
        (analyzer.COMBO, since),
    )
    found: list[dict] = []
    for r in rows:
        source = r["abort_source"] or legacy_source(r["abort_reason"])
        if source not in SAFETY_SOURCES:
            continue
        parts = _sorted(json.loads(r["params"]).get("parts") or [])
        if len(parts) < 2:
            continue
        found.append({
            "key": " + ".join(combo.limit_key(parts)), "parts": parts, "levels": _part_levels(parts), "label": _part_text(parts),
            "experiment_id": r["id"], "source": source, "at": r["created_at"],
        })
    keep = [
        x for i, x in enumerate(found)
        if not any(
            y["key"] == x["key"] and j != i and all(a <= b for a, b in zip(y["levels"], x["levels"]))
            and (y["levels"] != x["levels"] or j < i)
            for j, y in enumerate(found)
        )
    ]
    out = []
    for x in sorted(keep, key=lambda x: (x["key"], x["at"])):
        parts = [
            {"kind": combo.kind_of(p), "target": p["target"], "intensity": None if math.isinf(lv) else lv}
            for p, lv in zip(x["parts"], x["levels"])
        ]
        out.append({k: v for k, v in x.items() if k not in ("parts", "levels")} | {"parts": parts})
    return out


def combo_exclusions(cands, limits: list[dict]) -> tuple[list, list[dict]]:
    """Drop a combo candidate when an aborted combo of the same pair had every part at or below the candidate's level."""
    by: dict[str, list[dict]] = {}
    for x in limits:
        by.setdefault(x["key"], []).append(x)
    kept, out = [], []
    for c in cands:
        parts = _sorted(c.params.get("parts") or [])
        lv = _part_levels(parts)
        hit = next(
            (x for x in by.get(" + ".join(combo.limit_key(parts)), [])
             if all(a <= b for a, b in zip([math.inf if p["intensity"] is None else p["intensity"] for p in x["parts"]], lv))),
            None,
        )
        if hit is None:
            kept.append(c)
        else:
            out.append({
                "candidate": c.id, "target": c.target, "kind": "combo", "evidence": hit["experiment_id"],
                "reason": f"combo {hit['label']} aborted in {hit['experiment_id']}; {_part_text(parts)} excluded",
            })
    return kept, out
