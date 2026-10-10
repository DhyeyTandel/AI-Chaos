import json
import math
import time

import pytest

from faultline import analyzer, baseline as bl, runs
from faultline import graph as plan_graph
from faultline.agents import analyst, fixer
from test_planner import FakeChat, fresh_limiter, gmap  # noqa: F401


def jr(err=0.0, p95=100.0, base_err=0.0, base_p95=100.0):
    return {"count": 20, "error_rate": err, "p95_ms": p95, "base_error_rate": base_err, "base_p95_ms": base_p95}


def add_exp(store, eid, target, fault="delay", params=None, impact=None, recovery=2.0, state="COMPLETED", reason=None, injected=True, t=None):
    t = t or time.time()
    with store.conn:
        store.conn.execute(
            "INSERT INTO experiments (id, fault, target, params, state, created_at, injected_at, reverted_at, finished_at, abort_reason, recovery_seconds, impact)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (eid, fault, target, json.dumps(params or {"delay_ms": 100}), state, t, t if injected else None, t + 20 if injected else None,
             t + 30, reason, recovery, json.dumps(impact) if impact else None),
        )


def add_run(store, rows):
    rid = "run-test"
    runs.start(store, rid, len(rows))
    for i, r in enumerate(rows, 1):
        base = {"round": i, "tier": 1, "candidate_id": f"delay:{r['target']}:100ms", "fault": "delay", "params": {}, "llm_rank": 1,
                "reason": "why", "fallback": 1, "hypothesis": "h", "predicted_blast_radius": [], "outcome": "COMPLETED",
                "abort_reason": None, "recovery_s": 2.0, "climb_decision": "x"}
        runs.add_round(store, rid, {**base, **r})
    runs.update(store, rid, rounds_done=len(rows), ended_at=time.time() + 100, stop_reason="completed")
    return runs.get(store, rid)


def healthy():
    return {"home": jr(), "add_to_cart": jr()}


def seed_baseline(store, cfg):
    data = {"journeys": {"home": {"error_rate": 0.0, "p95_ms": 100.0}, "add_to_cart": {"error_rate": 0.0, "p95_ms": 100.0}},
            "thresholds": {"home": {"error_rate_max": 0.2, "p95_max_ms": 800}, "add_to_cart": {"error_rate_max": 0.2, "p95_max_ms": 800}}}
    bl.save(store, data, {})


def one(cfg, store, **kw):
    seed_baseline(store, cfg)
    add_exp(store, "e1", kw.pop("target", "cart"), **kw)
    run = add_run(store, [{"target": "cart", "experiment_id": "e1", **kw.get("rnd", {})}])
    return analyzer.analyze(cfg, store, gmap(), run, runs.rounds(store, "run-test"))


def test_degradation_normalisation(cfg):
    assert analyzer.journey_degradation(cfg, jr()) == 0
    assert analyzer.journey_degradation(cfg, jr(err=0.25)) == pytest.approx(0.5)
    assert analyzer.journey_degradation(cfg, jr(err=0.9)) == 1.0
    assert analyzer.journey_degradation(cfg, jr(p95=1000.0)) == pytest.approx(1.0)
    assert analyzer.journey_degradation(cfg, jr(p95=316.2278)) == pytest.approx(0.5, abs=1e-3)
    assert analyzer.journey_degradation(cfg, jr(p95=50.0)) == 0
    assert analyzer.journey_degradation(cfg, jr(p95=300.0, base_p95=10.0)) == pytest.approx(math.log(3) / math.log(10))


def test_penalty_math_and_never_recovered(cfg, store):
    imp = {"home": jr(err=0.5), "add_to_cart": jr()}
    a = one(cfg, store, impact=imp, recovery=30.0)["experiments"][0]
    assert a["impact"] == 1.0 and a["breadth"] == 0.5 and a["recovery_norm"] == 0.5
    assert a["penalty"] == pytest.approx(100 * (0.5 + 0.15 + 0.1))


def test_never_recovered_scores_full_recovery_component(cfg, store):
    a = one(cfg, store, impact={"home": jr(), "add_to_cart": jr()}, recovery=None)["experiments"][0]
    assert a["never_recovered"] and a["recovery_norm"] == 1.0 and a["penalty"] == pytest.approx(20.0)


def test_recovery_capped_and_not_injected(cfg):
    assert analyzer.recovery_norm(cfg, True, True, 600) == (1.0, False)
    assert analyzer.recovery_norm(cfg, False, False, None) == (0.0, False)


def test_service_and_overall_scores(cfg, store):
    seed_baseline(store, cfg)
    add_exp(store, "e1", "cart", impact={"home": jr(), "add_to_cart": jr(err=0.5)}, recovery=0.0)
    add_exp(store, "e2", "cart", impact=healthy(), recovery=0.0)
    add_exp(store, "e3", "quote", impact=healthy(), recovery=0.0)
    run = add_run(store, [{"target": "cart", "experiment_id": "e1"}, {"target": "cart", "experiment_id": "e2"}, {"target": "quote", "experiment_id": "e3"}])
    res = analyzer.analyze(cfg, store, gmap(), run, runs.rounds(store, "run-test"))
    assert res["services"]["cart"]["score"] == pytest.approx(100 - 65.0)
    assert res["services"]["quote"]["score"] == 100
    w = res["services"]["cart"]["weight"]
    assert w == 1 and res["services"]["quote"]["weight"] == 1
    assert res["overall"] == pytest.approx((35 + 100) / 2)
    assert "frontend" in res["untested"] and "cart" not in res["untested"] and "grafana" not in res["untested"]


def test_verdict_logic():
    assert analyzer.verdict({"a", "b"}, {"a", "b"}) == "CONFIRMED"
    assert analyzer.verdict(set(), set()) == "CONFIRMED"
    assert analyzer.verdict({"a", "b"}, {"a"}) == "PARTIAL"
    assert analyzer.verdict({"a"}, {"a", "c"}) == "PARTIAL"
    assert analyzer.verdict({"a"}, {"c"}) == "REFUTED"
    assert analyzer.verdict({"a"}, set()) == "REFUTED"
    assert analyzer.verdict(set(), {"c"}) == "REFUTED"


def test_prediction_uses_named_journeys_else_map():
    g = gmap()
    assert analyzer.predicted_journeys(g, "cart", ["frontend", "journey:home"])[0] == {"home"}
    assert analyzer.predicted_journeys(g, "cart", ["frontend"])[0] == {"add_to_cart"}


def test_hidden_dependency_detected(cfg, store):
    seed_baseline(store, cfg)
    add_exp(store, "e1", "quote", impact={"home": jr(p95=900.0), "add_to_cart": jr()}, recovery=1.0)
    add_exp(store, "e2", "cart", impact={"home": jr(), "add_to_cart": jr(p95=900.0)}, recovery=1.0)
    run = add_run(store, [{"target": "quote", "experiment_id": "e1", "predicted_blast_radius": ["frontend"]}, {"target": "cart", "experiment_id": "e2"}])
    res = analyzer.analyze(cfg, store, gmap(), run, runs.rounds(store, "run-test"))
    assert [h["experiment_id"] for h in res["hidden"]] == ["e1"]
    assert res["hidden"][0]["unexpected_journeys"] == ["home"] and "no journey" in res["hidden"][0]["map_note"]
    assert res["experiments"][0]["verdict"] == "REFUTED" or res["experiments"][0]["verdict"] == "PARTIAL"
    assert res["experiments"][1]["verdict"] == "CONFIRMED" and not res["experiments"][1]["hidden_journeys"]


def test_history_experiments_included_and_flagged(cfg, store):
    seed_baseline(store, cfg)
    add_exp(store, "old", "flagd", impact={"home": jr(), "add_to_cart": jr(p95=3000.0)}, t=time.time() - 500)
    add_exp(store, "e1", "cart", impact=healthy())
    run = add_run(store, [{"target": "cart", "experiment_id": "e1"}])
    res = analyzer.analyze(cfg, store, gmap(), run, runs.rounds(store, "run-test"))
    by = {a["experiment_id"]: a for a in res["experiments"]}
    assert by["old"]["scope"] == "history" and by["e1"]["scope"] == "run"
    assert res["hidden"][0]["target"] == "flagd" and res["hidden"][0]["scope"] == "history"
    assert res["services"]["flagd"]["weight"] >= 1


def test_aborted_before_injection_has_no_score(cfg, store):
    seed_baseline(store, cfg)
    add_exp(store, "e1", "cart", state="ABORTED", reason="preflight: x", injected=False, recovery=None)
    run = add_run(store, [{"target": "cart", "experiment_id": "e1", "outcome": "ABORTED"}])
    res = analyzer.analyze(cfg, store, gmap(), run, runs.rounds(store, "run-test"))
    assert res["experiments"][0]["verdict"] == "n/a" and res["overall"] is None and not res["services"]


def sample_analysis(cfg, store):
    seed_baseline(store, cfg)
    add_exp(store, "e1", "cart", impact={"home": jr(), "add_to_cart": jr(err=0.6, p95=2000.0)}, recovery=None)
    add_exp(store, "e2", "quote", impact={"home": jr(p95=900.0), "add_to_cart": jr()}, recovery=3.0)
    add_exp(store, "e3", "frontend", impact=healthy(), recovery=1.0)
    run = add_run(store, [{"target": t, "experiment_id": e} for e, t in (("e1", "cart"), ("e2", "quote"), ("e3", "frontend"))])
    return run, runs.rounds(store, "run-test"), analyzer.analyze(cfg, store, gmap(), run, runs.rounds(store, "run-test"))


def test_analyst_validation_drops_unknown_refs(cfg, store):
    run, rounds, an = sample_analysis(cfg, store)
    out = {"weaknesses": [
        {"rank": 1, "title": "ghost", "severity": "HIGH", "services": ["cart"], "explanation": "x", "evidence": "y", "related_experiment_ids": ["nope"]},
        {"rank": 2, "title": "phantom svc", "severity": "low", "services": ["nosuch"], "explanation": "x", "evidence": "y", "related_experiment_ids": ["e1"]},
        {"rank": 3, "title": "ungrounded", "severity": "low", "services": ["cart"], "explanation": "x", "evidence": "y", "related_experiment_ids": []},
        {"rank": 4, "title": "cart single point", "severity": "Critical", "services": ["cart"], "explanation": "e" * 900, "evidence": "v" * 900, "related_experiment_ids": ["e1"]},
    ], "executive_summary": "s" * 900}
    res = analyst.analyze(cfg, store, an, {"cart", "quote"}, chat=FakeChat(lambda n: json.dumps(out)))
    assert not res.fallback and res.dropped == 3
    assert [(w.rank, w.title, w.severity) for w in res.weaknesses] == [(1, "cart single point", "critical")]
    assert len(res.weaknesses[0].explanation) == 400 and len(res.weaknesses[0].evidence) == 300 and len(res.executive_summary) == 600


def test_analyst_all_invalid_falls_back_rule_based(cfg, store):
    run, rounds, an = sample_analysis(cfg, store)
    out = {"weaknesses": [{"rank": 1, "title": "t", "severity": "low", "services": ["zzz"], "explanation": "x", "related_experiment_ids": ["e1"]}], "executive_summary": "s"}
    res = analyst.analyze(cfg, store, an, {"cart"}, chat=FakeChat(lambda n: json.dumps(out)))
    assert res.fallback and res.weaknesses and res.weaknesses[0].source == "fallback"
    penalties = [next(a["penalty"] for a in an["experiments"] if a["experiment_id"] == w.related_experiment_ids[0]) for w in res.weaknesses]
    assert penalties == sorted(penalties, reverse=True)


def test_fixer_override_validation(cfg):
    ok = "services:\n  cart:\n    restart: unless-stopped\n    deploy:\n      resources:\n        limits:\n          cpus: '0.5'\n"
    assert fixer.check_override(ok, {"cart"})[1] is None
    assert fixer.check_override("services:\n  cart:\n    image: evil\n", {"cart"})[0] is None
    assert fixer.check_override("services:\n  cart:\n    ports: ['1:1']\n", {"cart"})[1]
    assert fixer.check_override("services:\n  cart:\n    deploy:\n      replicas: 3\n", {"cart"})[0] is None
    assert fixer.check_override("services:\n  ghost:\n    restart: always\n", {"cart"})[1] == "unknown service ghost"
    assert fixer.check_override("services:\n  cart:\n    restart: sometimes\n", {"cart"})[0] is None
    assert fixer.check_override("a: [unclosed", {"cart"})[1] == "not valid YAML"
    assert fixer.check_override("just text", {"cart"})[0] is None


def test_fixer_drops_bad_override_keeps_fix_and_fills_gaps(cfg, store):
    run, rounds, an = sample_analysis(cfg, store)
    ws = [analyst.Weakness(rank=1, title="a", severity="high", services=["cart"], explanation="x", related_experiment_ids=["e1"]),
          analyst.Weakness(rank=2, title="b", severity="low", services=["quote"], explanation="x", related_experiment_ids=["e2"])]
    out = {"fixes": [{"weakness_rank": 1, "fix_title": "t", "fix_type": "config", "steps": "do it", "compose_override": "services:\n  cart:\n    privileged: true\n", "expected_effect": "e"}]}
    res = fixer.fix(cfg, store, an, gmap(), ws, chat=FakeChat(lambda n: json.dumps(out)))
    assert not res.fallback and res.llm_fixes == 1 and res.filled == 1
    f1, f2 = res.fixes
    assert f1.compose_override is None and f1.override_dropped and f1.steps == "do it"
    assert f2.source == "fallback" and f2.weakness_rank == 2


def test_fixer_fallback_catalog_overrides_are_valid(cfg):
    g = gmap()
    for svc in ("cart", "valkey-cart", "frontend", "quote"):
        for kind in ("crash", "delay"):
            w = analyst.Weakness(rank=1, title="t", severity="low", services=[svc], explanation="x")
            f = fixer.fallback_fix(g, w, kind)
            if f.compose_override:
                assert fixer.check_override(f.compose_override, set(g["nodes"]))[1] is None


def test_sanitise_dashes():
    assert analyzer.clean("a \u2014 b") == "a - b"
    assert analyzer.clean("a\u2014b") == "a - b"
    assert analyzer.clean("x\u2011y \u2013 z\u202fq") == "x-y - z q"
    assert analyzer.clean_deep({"k": ["a\u2014b", {"n": "c\u2011d"}], "i": 3}) == {"k": ["a - b", {"n": "c-d"}], "i": 3}
    assert analyzer.clean("it\u2019s \u201cok\u201d") == "it's \"ok\""
    assert analyzer.clean("services:\n  a:\n    - x") == "services:\n  a:\n    - x"


def test_fallback_report_renders_all_sections(cfg, store, tmp_path, monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    run, rounds, an = sample_analysis(cfg, store)
    state = plan_graph.run_report(cfg, gmap(), store, tmp_path, "run-test")
    md = (tmp_path / "run-test" / "report.md").read_text()
    for h in ("# Faultline resilience report", "## Executive summary", "## Ranked weaknesses and proposed fixes", "## Experiments in this run",
              "## Hidden dependencies", "## Per-service scores", "## Score formula", "## Methodology", "## AI usage"):
        assert h in md
    assert "fallback" in md and "cart" in md and "\u2014" not in md and "\u2011" not in md
    html_text = (tmp_path / "run-test" / "report.html").read_text()
    assert "<script" not in html_text and "prefers-color-scheme:dark" in html_text and "Per-service scores" in html_text
    data = json.loads((tmp_path / "run-test" / "report.json").read_text())
    assert data["overall_score"] == state["data"]["overall_score"] and data["weaknesses"] and data["weaknesses"][0]["fix"]
    assert {u["agent"] for u in data["ai_usage"]} == {"planner", "safety", "analyst", "fixer"}
    assert all("fallback" in u["path"] or u["path"] == "not run" for u in data["ai_usage"])


def test_report_latest_run_and_missing(cfg, store, tmp_path):
    assert plan_graph.run_report(cfg, gmap(), store, tmp_path) is None
    sample_analysis(cfg, store)
    assert plan_graph.run_report(cfg, gmap(), store, tmp_path)["data"]["run"]["run_id"] == "run-test"


def test_llm_report_with_dashes_is_sanitised(cfg, store, tmp_path):
    run, rounds, an = sample_analysis(cfg, store)
    a_out = {"weaknesses": [{"rank": 1, "title": "cart \u2014 SPOF", "severity": "high", "services": ["cart"], "explanation": "bad\u2011thing \u2014 yes",
                             "evidence": "e1", "related_experiment_ids": ["e1"]}], "executive_summary": "sum \u2014 mary"}
    f_out = {"fixes": [{"weakness_rank": 1, "fix_title": "fix\u2014it", "fix_type": "code", "steps": "s", "expected_effect": "ok"}]}
    chat = FakeChat(lambda n: json.dumps(a_out if n == 1 else f_out))
    state = plan_graph.run_report(cfg, gmap(), store, tmp_path, chat=chat)
    for name in ("report.md", "report.html", "report.json"):
        text = (tmp_path / "run-test" / name).read_text()
        assert "\u2014" not in text and "\u2011" not in text
    assert "cart - SPOF" in (tmp_path / "run-test" / "report.md").read_text()
    usage = {u["agent"]: u for u in state["data"]["ai_usage"]}
    assert usage["analyst"]["path"].startswith("groq") and usage["fixer"]["path"].startswith("groq")


def test_fixer_override_bare_service_body_is_wrapped(cfg):
    out, err = fixer.check_override("restart: on-failure\ndeploy:\n  resources:\n    limits:\n      cpus: '0.2'\n", {"cart"}, "cart")
    assert err is None and out.startswith("services:\n  cart:\n    restart: on-failure")
    assert fixer.check_override("restart: on-failure\n", {"cart"})[0] is None
    assert fixer.check_override("image: x\n", {"cart"}, "cart")[0] is None


def test_report_shows_learned_limits_and_safety_review(cfg, store, tmp_path, monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    sample_analysis(cfg, store)
    pf = {"decision": "reduce", "reason": "close to a known limit", "fallback": 0, "error": None,
          "original": {"params": {"delay_ms": 300.0}, "duration_s": 20.0}, "final": {"params": {"delay_ms": 100.0}, "duration_s": 20.0},
          "vetoed": [{"candidate": "crash:quote:20s", "reason": "repeats a known abort"}]}
    with store.conn:
        store.conn.execute("UPDATE run_rounds SET preflight = ? WHERE round = 1", (json.dumps(pf),))
        store.conn.execute("UPDATE experiments SET preflight_review = ? WHERE id = 'e1'", (json.dumps(pf),))
    reason = "delay on quote aborted at 100 ms in exp-old; 300 ms excluded"
    limit = {"target": "quote", "kind": "delay", "intensity": 100.0, "label": "100 ms", "experiment_id": "exp-old", "source": "watchdog", "at": 0}
    runs.update(store, "run-test", memory={"limits": [limit], "exclusions": [{"candidate": "delay:quote:300ms", "reason": reason, "evidence": "exp-old"}]})
    state = plan_graph.run_report(cfg, gmap(), store, tmp_path, "run-test")
    md = (tmp_path / "run-test" / "report.md").read_text()
    assert "## Learned limits" in md and reason in md and "exp-old" in md and "100 ms" in md
    assert "reduced delay 300 -> 100 ms: close to a known limit" in md and "Safety review" in md
    assert "crash:quote:20s (repeats a known abort)" in md and "Pre-flight review: 1 reviews" in md and "1 vetoes" in md
    assert state["data"]["learned_limits"]["limits"][0]["experiment_id"] == "exp-old"
