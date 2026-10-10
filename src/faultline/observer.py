import os
import time

import httpx


def jaeger_base() -> str:
    return os.environ.get("FAULTLINE_JAEGER", "http://jaeger:16686/jaeger/ui").rstrip("/")


def prom_base() -> str:
    return os.environ.get("FAULTLINE_PROMETHEUS", "http://prometheus:9090").rstrip("/")


def jaeger_dependencies(lookback_min: int, client=None) -> dict[tuple[str, str], int]:
    c = client or httpx.Client(timeout=15)
    r = c.get(
        f"{jaeger_base()}/api/dependencies",
        params={"endTs": int(time.time() * 1000), "lookback": lookback_min * 60_000},
    )
    r.raise_for_status()
    return {(d["parent"], d["child"]): int(d.get("callCount", 0)) for d in r.json().get("data") or []}


def jaeger_traces(service: str, lookback_min: int, limit: int = 100, client=None) -> list[dict]:
    c = client or httpx.Client(timeout=30)
    r = c.get(
        f"{jaeger_base()}/api/traces",
        params={"service": service, "lookback": f"{lookback_min}m", "limit": limit},
    )
    r.raise_for_status()
    return r.json().get("data") or []


def jaeger_services(client=None) -> list[str]:
    c = client or httpx.Client(timeout=15)
    r = c.get(f"{jaeger_base()}/api/services")
    r.raise_for_status()
    return r.json().get("data") or []


def edges_from_traces(traces: list[dict]) -> dict[tuple[str, str], int]:
    out: dict[tuple[str, str], int] = {}
    for t in traces:
        procs = t.get("processes", {})
        svc = {s["spanID"]: procs.get(s["processID"], {}).get("serviceName") for s in t.get("spans", [])}
        for s in t.get("spans", []):
            child = svc.get(s["spanID"])
            for ref in s.get("references", []):
                parent = svc.get(ref["spanID"])
                if parent and child and parent != child:
                    out[(parent, child)] = out.get((parent, child), 0) + 1
    return out


def prom_query(q: str, client=None) -> list[dict]:
    c = client or httpx.Client(timeout=15)
    r = c.get(f"{prom_base()}/api/v1/query", params={"query": q})
    r.raise_for_status()
    return r.json()["data"]["result"]


def prom_service_stats(window_s: int = 120, client=None) -> dict[str, dict]:
    w = f"{int(window_s)}s"
    calls = f"sum by (service_name) (rate(traces_span_metrics_calls_total[{w}]))"
    errs = f'sum by (service_name) (rate(traces_span_metrics_calls_total{{status_code="STATUS_CODE_ERROR"}}[{w}]))'
    p95 = (
        "histogram_quantile(0.95, sum by (le, service_name) "
        f"(rate(traces_span_metrics_duration_milliseconds_bucket[{w}])))"
    )
    out: dict[str, dict] = {}
    for key, q in (("rate", calls), ("err_rate", errs), ("p95_ms", p95)):
        for r in prom_query(q, client):
            v = float(r["value"][1])
            if v == v:
                out.setdefault(r["metric"].get("service_name", "?"), {})[key] = v
    for d in out.values():
        if d.get("rate"):
            d["error_ratio"] = d.get("err_rate", 0.0) / d["rate"]
    return out
