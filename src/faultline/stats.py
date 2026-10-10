import math
from dataclasses import dataclass


@dataclass(frozen=True)
class JourneyStats:
    count: int
    success_rate: float | None
    p50: float | None
    p95: float | None


def percentile(values, p: float):
    """Nearest-rank percentile: the value at rank ceil(p/100 * n) of the sorted data.
    Returns None for empty input."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(p / 100 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def journey_stats(store, journey: str, since: float | None = None, until: float | None = None) -> JourneyStats:
    rows = store.query(journey=journey, since=since, until=until)
    if not rows:
        return JourneyStats(0, None, None, None)
    lat = [r["latency_ms"] for r in rows]
    ok = sum(1 for r in rows if r["ok"])
    return JourneyStats(len(rows), ok / len(rows), percentile(lat, 50), percentile(lat, 95))
