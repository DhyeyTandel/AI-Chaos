from faultline import mapper as m
from faultline.observer import edges_from_traces

NODES = {"frontend", "cart", "valkey-cart", "checkout", "kafka", "flagd", "otel-collector"}


def containers():
    return [
        {"service": "checkout", "env": {"CART_ADDR": "cart:7070", "KAFKA_ADDR": "kafka:9092", "FLAGD_HOST": "flagd", "EMAIL_ADDR": "http://email:6060", "OTEL_EXPORTER_OTLP_ENDPOINT": "http://otel-collector:4318"}, "depends_on": ["otel-collector"]},
        {"service": "cart", "env": {"VALKEY_ADDR": "valkey-cart:6379"}, "depends_on": ["valkey-cart"]},
    ]


def test_normalize_host():
    assert m.normalize_host("http://shipping:50050/x") == "shipping"
    assert m.normalize_host("kafka:9092") == "kafka"
    assert m.normalize_host("flagd") == "flagd"


def test_static_edges_env_and_depends():
    e = m.static_edges(containers(), NODES)
    assert ("checkout", "cart") in e and ("checkout", "kafka") in e and ("checkout", "flagd") in e
    assert ("checkout", "email") not in e
    assert e[("cart", "valkey-cart")] == {"env:VALKEY_ADDR", "depends_on"}
    assert ("checkout", "otel-collector") in e and e[("checkout", "otel-collector")] == {"depends_on"}


def test_parse_depends_on():
    assert m.parse_depends_on("a:service_started:false,b:service_healthy:false") == ["a", "b"]
    assert m.parse_depends_on(None) == []


def test_merge_origins():
    static = m.static_edges(containers(), NODES)
    dynamic = {("checkout", "cart"): 5, ("frontend", "flagd"): 3}
    g = m.merge(static, dynamic, set(NODES))
    by = {(e["source"], e["target"]): e for e in g["edges"]}
    assert by[("checkout", "cart")]["origin"] == "both" and by[("checkout", "cart")]["calls"] == 5
    assert by[("frontend", "flagd")]["origin"] == "traces"
    assert by[("checkout", "kafka")]["origin"] == "static"
    assert g["nodes"]["kafka"]["stateful"] and not g["nodes"]["cart"]["stateful"]
    h = m.hidden_report(g, True)
    assert h["traces_only"] == ["frontend -> flagd"] and "checkout -> kafka" in h["static_only"]
    assert m.hidden_report(g, False)["static_only"] == []


def test_overrides_add_remove():
    static = m.static_edges(containers(), NODES)
    ov = {"add_edges": [{"source": "frontend", "target": "checkout"}], "remove_edges": [{"source": "checkout", "target": "otel-collector"}], "stateful": ["cart"]}
    g = m.merge(static, {}, set(NODES), ov)
    by = {(e["source"], e["target"]): e for e in g["edges"]}
    assert by[("frontend", "checkout")]["origin"] == "override"
    assert ("checkout", "otel-collector") not in by
    assert g["nodes"]["cart"]["stateful"]


def test_overrides_file(tmp_path):
    assert m.load_overrides(tmp_path / "none.yaml") == {}
    p = tmp_path / "o.yaml"
    p.write_text("add_edges: [{source: a, target: b}]\n")
    assert m.load_overrides(p)["add_edges"][0]["target"] == "b"


def trace(path, services):
    procs = {f"p{i}": {"serviceName": s} for i, s in enumerate(services)}
    spans = [{"spanID": f"s{i}", "processID": f"p{i}", "references": [{"spanID": f"s{i-1}"}] if i else [], "tags": []} for i in range(len(services))]
    spans[0]["tags"] = [{"key": "http.url", "value": f"http://frontend-proxy:8080{path}?currencyCode=USD"}]
    return {"spans": spans, "processes": procs}


def test_edges_from_traces_and_journeys():
    traces = [trace("/api/checkout", ["frontend-proxy", "frontend", "checkout"]), trace("/api/cart", ["frontend-proxy", "frontend", "cart"])]
    assert edges_from_traces(traces) == {("frontend-proxy", "frontend"): 2, ("frontend", "checkout"): 1, ("frontend", "cart"): 1}
    j = m.journeys_from_traces(traces)
    assert j["checkout"] == {"frontend-proxy", "frontend", "checkout"}
    static = m.static_edges(containers(), NODES)
    g = m.merge(static, {}, set(NODES))
    out = m.derive_journeys(j, g)
    assert out["checkout"]["source"] == "static+traces" and out["home"]["source"] == "static"
    assert "kafka" in out["checkout"]["services"]
    assert "valkey-cart" in out["cart"]["services"] and out["checkout"]["traced_extra"] == []


def test_journey_of_path():
    assert m.journey_of_path("/") == "home" and m.journey_of_path("/api/products") == "home"
    assert m.journey_of_path("/api/products/OLJCESPC7Z") == "product"
    assert m.journey_of_path("/api/recommendations") == "product"
    assert m.journey_of_path("/other") is None


def test_product_journey_includes_recommendation():
    assert "recommendation" in m.STATIC_JOURNEYS["product"]
    assert "recommendation" not in m.STATIC_JOURNEYS["home"]


def test_mermaid():
    g = m.merge(m.static_edges(containers(), NODES), {}, set(NODES))
    out = m.mermaid(g)
    assert out.startswith("graph LR") and "valkey_cart" in out and "checkout -.-> cart" in out


def test_self_edges_dropped_and_external_kind():
    g = m.merge({}, {("a", "a"): 3, ("a", "browser"): 1}, {"a", "browser"}, known={"a"})
    assert [(e["source"], e["target"]) for e in g["edges"]] == [("a", "browser")]
    assert g["nodes"]["browser"]["kind"] == "external" and g["nodes"]["a"]["kind"] == "app"
