from dataclasses import dataclass

from .baseline import done_ts
from .stats import percentile

OK, BREACH, BLIND = "ok", "breach", "blind"


@dataclass(frozen=True)
class Decision:
    status: str
    reason: str = ""


def _window(rows, journey, now, window_s, since=None):
    lo = now - window_s if since is None else max(since, now - window_s)
    return [r for r in rows if r["journey"] == journey and lo <= done_ts(r) <= now]


def judge(rows, thresholds, now, window_s, min_samples, journeys=None, since=None):
    """Return (breaches, judged, short): breach strings, journeys judged, journeys lacking samples."""
    breaches, judged, short = [], [], []
    for j in journeys or thresholds:
        w = _window(rows, j, now, window_s, since)
        if len(w) < min_samples:
            short.append(j)
            continue
        judged.append(j)
        t = thresholds[j]
        err = sum(1 for r in w if not r["ok"]) / len(w)
        p95 = percentile([r["latency_ms"] for r in w], 95)
        if err > t["error_rate_max"]:
            breaches.append(f"{j} error rate {err:.0%} > {t['error_rate_max']:.0%}")
        if p95 > t["p95_max_ms"]:
            breaches.append(f"{j} p95 {p95:.0f}ms > {t['p95_max_ms']:.0f}ms")
    return breaches, judged, short


def evaluate(rows, thresholds, now, started_at, cfg) -> Decision:
    w = cfg.watchdog
    last = max([done_ts(r) for r in rows if done_ts(r) <= now], default=started_at)
    last = max(last, started_at)
    if now - last > w.blind_s:
        return Decision(BLIND, f"no prober samples for {now - last:.1f}s")
    breaches, _, _ = judge(rows, thresholds, now, w.window_s, w.min_samples)
    if breaches:
        return Decision(BREACH, "; ".join(breaches))
    return Decision(OK)


class Watchdog:
    def __init__(self, thresholds, cfg, started_at):
        self.thresholds = thresholds
        self.cfg = cfg
        self.started_at = started_at
        self.streak = 0

    def check(self, rows, now) -> Decision:
        d = evaluate(rows, self.thresholds, now, self.started_at, self.cfg)
        if d.status == BLIND:
            return d
        if d.status == BREACH:
            self.streak += 1
            if self.streak >= self.cfg.watchdog.breach_ticks:
                return d
            return Decision(OK, "breach pending")
        self.streak = 0
        return d


def recovered(rows, thresholds, now, since, cfg) -> bool:
    r = cfg.recovery
    breaches, judged, short = judge(rows, thresholds, now, r.window_s, r.min_samples, since=since)
    return not breaches and not short


def impact(rows, baseline: dict, t0: float, t1: float) -> dict:
    out = {}
    for j, base in baseline["journeys"].items():
        w = [r for r in rows if r["journey"] == j and t0 <= done_ts(r) <= t1]
        n = len(w)
        out[j] = {
            "count": n,
            "error_rate": (sum(1 for r in w if not r["ok"]) / n) if n else None,
            "p95_ms": percentile([r["latency_ms"] for r in w], 95),
            "base_error_rate": base["error_rate"],
            "base_p95_ms": base["p95_ms"],
        }
    return out
