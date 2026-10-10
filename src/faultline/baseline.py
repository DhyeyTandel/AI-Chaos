import json
import math
import statistics
import time

from .stats import percentile


def done_ts(row) -> float:
    return row["ts_wall"] + row["latency_ms"] / 1000


def journey_summary(rows: list[dict]) -> dict:
    lat = [r["latency_ms"] for r in rows]
    n = len(rows)
    ok = sum(1 for r in rows if r["ok"])
    return {
        "count": n,
        "success_rate": ok / n if n else None,
        "error_rate": 1 - ok / n if n else None,
        "p50_ms": percentile(lat, 50),
        "p95_ms": percentile(lat, 95),
        "mean_ms": statistics.fmean(lat) if lat else None,
        "stddev_ms": statistics.pstdev(lat) if len(lat) > 1 else 0.0 if lat else None,
    }


def thresholds_for(summary: dict, cfg) -> dict:
    t = cfg.thresholds
    n_w = max(1, int(cfg.watchdog.window_s))
    p = summary["error_rate"] or 0.0
    sigma = math.sqrt(p * (1 - p) / n_w)
    return {
        "error_rate_max": max(t.error_floor, p + t.k_sigma * sigma),
        "p95_max_ms": max(t.p95_floor_ms, (summary["p95_ms"] or 0.0) * t.p95_multiplier),
    }


def compute(rows_by_journey: dict[str, list[dict]], cfg) -> dict:
    journeys, thresholds = {}, {}
    for j, rows in rows_by_journey.items():
        s = journey_summary(rows)
        journeys[j] = s
        thresholds[j] = thresholds_for(s, cfg)
    return {"journeys": journeys, "thresholds": thresholds}


def save(store, data: dict, params: dict) -> int:
    with store.conn:
        cur = store.conn.execute(
            "INSERT INTO baselines (ts, params, data) VALUES (?, ?, ?)",
            (time.time(), json.dumps(params), json.dumps(data)),
        )
    return cur.lastrowid


def latest(store) -> dict | None:
    r = store.conn.execute("SELECT * FROM baselines ORDER BY id DESC LIMIT 1").fetchone()
    if not r:
        return None
    return {"id": r["id"], "ts": r["ts"], "params": json.loads(r["params"]), **json.loads(r["data"])}
