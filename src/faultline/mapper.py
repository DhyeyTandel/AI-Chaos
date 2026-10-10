import json
import time
from pathlib import Path

import yaml


STATEFUL = frozenset({"astronomy-db", "valkey-cart", "kafka"})
INFRA = frozenset({"otel-collector", "jaeger", "prometheus", "opamp-server", "telemetry-docs"})
TOOLING = frozenset({"faultline", "reaper", "load-generator"})
ENV_SUFFIXES = ("_ADDR", "_HOST")

STATIC_JOURNEYS = {
    "home": ["frontend-proxy", "frontend", "product-catalog", "currency", "astronomy-db"],
    "product": ["frontend-proxy", "frontend", "product-catalog", "currency", "astronomy-db", "recommendation"],
    "cart": ["frontend-proxy", "frontend", "cart", "valkey-cart", "product-catalog", "currency"],
    "checkout": [
        "frontend-proxy", "frontend", "checkout", "cart", "valkey-cart", "product-catalog",
        "currency", "shipping", "quote", "payment", "email", "kafka",
    ],
}
CLIENT_SERVICES = frozenset({"load-generator", "frontend-web"})
URL_KEYS = ("http.url", "url.full", "http.target", "url.path", "http.route", "http.path")


def normalize_host(value: str) -> str:
    v = value.strip().split("://", 1)[-1].split("/", 1)[0].rsplit("@", 1)[-1]
    return v.split(":", 1)[0]


def static_edges(containers: list[dict], known: set[str]) -> dict[tuple[str, str], set[str]]:
    edges: dict[tuple[str, str], set[str]] = {}
    for c in containers:
        src = c["service"]
        for k, v in c.get("env", {}).items():
            if k.endswith(ENV_SUFFIXES):
                dst = normalize_host(v)
                if dst in known and dst != src:
                    edges.setdefault((src, dst), set()).add(f"env:{k}")
        for dst in c.get("depends_on", []):
            if dst in known and dst != src:
                edges.setdefault((src, dst), set()).add("depends_on")
    return edges


def parse_depends_on(label: str | None) -> list[str]:
    return [p.split(":", 1)[0] for p in (label or "").split(",") if p.strip()]


def load_overrides(path) -> dict:
    p = Path(path)
    if not p.is_file():
        return {}
    return yaml.safe_load(p.read_text()) or {}


def merge(static: dict, dynamic: dict, nodes: set[str], overrides: dict | None = None, known: set[str] | None = None) -> dict:
    overrides = overrides or {}
    edges: dict[tuple[str, str], dict] = {}
    for k, via in static.items():
        edges[k] = {"origin": "static", "via": sorted(via), "calls": 0}
    for k, n in dynamic.items():
        if k[0] == k[1]:
            continue
        if k in edges:
            edges[k].update(origin="both", calls=n)
        else:
            edges[k] = {"origin": "traces", "via": [], "calls": n}
    for e in overrides.get("add_edges") or []:
        k = (e["source"], e["target"])
        nodes.update(k)
        if k in edges:
            edges[k]["via"] = sorted(set(edges[k]["via"]) | {"override"})
        else:
            edges[k] = {"origin": "override", "via": ["override"], "calls": 0}
    for e in overrides.get("remove_edges") or []:
        edges.pop((e["source"], e["target"]), None)
    stateful = set(STATEFUL) | set(overrides.get("stateful") or [])
    node_attrs = {
        n: {
            "stateful": n in stateful,
            "kind": "infra" if n in INFRA else "tooling" if n in TOOLING else "external" if known is not None and n not in known else "app",
        }
        for n in sorted(nodes)
    }
    return {
        "nodes": node_attrs,
        "edges": [{"source": s, "target": t, **a} for (s, t), a in sorted(edges.items())],
    }


def hidden_report(graph: dict, observed: bool) -> dict:
    traces_only = [f"{e['source']} -> {e['target']}" for e in graph["edges"] if e["origin"] == "traces"]
    static_only = [f"{e['source']} -> {e['target']}" for e in graph["edges"] if e["origin"] == "static" and e["target"] not in INFRA]
    return {"traces_only": traces_only, "static_only": static_only if observed else []}


def _url_path(value: str) -> str:
    v = value.split("://", 1)[-1]
    v = "/" + v.split("/", 1)[1] if "/" in v else "/"
    return v.split("?", 1)[0]


def journey_of_path(path: str) -> str | None:
    if path in ("/", "/api/products"):
        return "home"
    if path.startswith("/api/products/") or path == "/api/recommendations":
        return "product"
    if path == "/api/cart":
        return "cart"
    if path == "/api/checkout":
        return "checkout"
    return None


def journeys_from_traces(traces: list[dict]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for t in traces:
        procs = t.get("processes", {})
        found = set()
        for s in t.get("spans", []):
            for tag in s.get("tags", []):
                if tag.get("key") in URL_KEYS and isinstance(tag.get("value"), str):
                    j = journey_of_path(_url_path(tag["value"]))
                    if j:
                        found.add(j)
        if len(found) != 1:
            continue
        svcs = {procs.get(s["processID"], {}).get("serviceName") for s in t.get("spans", [])}
        out.setdefault(found.pop(), set()).update(x for x in svcs if x and x not in CLIENT_SERVICES)
    return out


def derive_journeys(traced: dict[str, set[str]], graph: dict) -> dict:
    out = {}
    for j, static in STATIC_JOURNEYS.items():
        if j in traced and traced[j]:
            svcs = set(traced[j]) | set(static)
            for e in graph["edges"]:
                if e["source"] in svcs and graph["nodes"].get(e["target"], {}).get("stateful"):
                    svcs.add(e["target"])
            out[j] = {"services": sorted(svcs), "source": "static+traces", "traced_extra": sorted(set(traced[j]) - set(static))}
        else:
            out[j] = {"services": sorted(static), "source": "static", "traced_extra": []}
    return out


def mermaid(graph: dict) -> str:
    lines = ["graph LR"]
    for n, a in graph["nodes"].items():
        ident = n.replace("-", "_")
        lines.append(f'  {ident}[("{n}")]' if a["stateful"] else f'  {ident}["{n}"]')
    arrow = {"static": "-.->", "traces": "==>", "both": "-->", "override": "-.->"}
    for e in graph["edges"]:
        s, t = e["source"].replace("-", "_"), e["target"].replace("-", "_")
        lines.append(f"  {s} {arrow[e['origin']]} {t}")
    return "\n".join(lines)


def dump(graph: dict, path) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(graph, indent=2))



def build_map(cfg, overrides_path, out_path, client=None) -> dict:
    from . import docker_api, observer

    details = [d for d in docker_api.container_details(client) if d["service"] not in ("faultline", "reaper")]
    known = {d["service"] for d in details}
    static = static_edges(details, known)
    notes = []
    dynamic, dyn_source = {}, None
    try:
        dynamic = observer.jaeger_dependencies(cfg.map.lookback_min)
        dyn_source = "jaeger /api/dependencies"
    except Exception as e:
        notes.append(f"dependencies API failed: {type(e).__name__}")
    if not dynamic:
        try:
            for svc in observer.jaeger_services():
                for k, n in observer.edges_from_traces(observer.jaeger_traces(svc, cfg.map.lookback_min, 30)).items():
                    dynamic[k] = dynamic.get(k, 0) + n
            dyn_source = "jaeger traces (parent/child spans)"
        except Exception as e:
            notes.append(f"trace fallback failed: {type(e).__name__}")
    nodes = set(known) | {n for k in dynamic for n in k}
    graph = merge(static, dynamic, nodes, load_overrides(overrides_path), known)
    traced = {}
    try:
        seen = {}
        for svc, n in (("frontend-proxy", 200), ("checkout", 40), ("cart", 40)):
            for t in observer.jaeger_traces(svc, cfg.map.lookback_min, n):
                seen[t["traceID"]] = t
        traced = journeys_from_traces(list(seen.values()))
    except Exception as e:
        notes.append(f"journey traces failed: {type(e).__name__}")
    graph["journeys"] = derive_journeys(traced, graph)
    graph["hidden"] = hidden_report(graph, bool(dynamic))
    graph["dynamic_source"] = dyn_source
    graph["notes"] = notes
    graph["generated_at"] = time.time()
    dump(graph, out_path)
    return graph
