import asyncio
import json
import time

import pytest

from faultline import analyzer, llm, memory, runs
from faultline.agents import analyst, fixer, planner, safety
from faultline import experiment as exp_mod
from test_experiment import Sim, SimDriver, ecfg, go as exp_go  # noqa: F401
from test_planner import FakeChat, FakeMsg, fresh_limiter, gmap, top_ids  # noqa: F401
from test_report import add_exp, add_run, jr, one, sample_analysis, seed_baseline  # noqa: F401


@pytest.fixture(autouse=True)
def no_key(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)


class Err400(Exception):
    status_code = 400

    def __init__(self, msg="Failed to generate JSON. Please adjust your prompt. See 'failed_generation' for more details."):
        super().__init__(msg)


class Script(FakeChat):
    """Each call returns the next item; an exception instance is raised."""

    def __init__(self, items):
        super().__init__(None)
        self.items, self.seen = list(items), []

    def invoke(self, msgs):
        self.calls += 1
        self.seen.append(msgs)
        item = self.items.pop(0) if self.items else self.items_last
        self.items_last = item
        if isinstance(item, Exception):
            raise item
        return FakeMsg(item)


def ranking(ids, key="candidate_id", **extra):
    return json.dumps({"ranking": [{key: c, "rank": i + 1, "reason": "why", **extra} for i, c in enumerate(ids)],
                       "hypothesis": "h", "predicted_blast_radius": []})


# planner

def test_context_numbers_candidates(cfg, store):
    chat = Script([ranking(top_ids(cfg, store))])
    planner.plan(cfg, gmap(), store, chat=chat)
    user = chat.seen[0][1][1]
    ids = top_ids(cfg, store)
    assert f"1. {ids[0]} (target" in user and f"{len(ids)}. {ids[-1]} (target" in user


def test_index_only_ranking_accepted(cfg, store):
    ids = top_ids(cfg, store)
    out = json.dumps({"ranking": [{"index": 3, "candidate_id": "made:up:1s", "rank": 1, "reason": "a"},
                                  {"index": "1", "rank": 2, "reason": "b"}], "hypothesis": "h", "predicted_blast_radius": []})
    res = planner.plan(cfg, gmap(), store, chat=Script([out]))
    assert not res.fallback
    assert [s.candidate.id for s in res.ranked[:2]] == [ids[2], ids[0]]


def test_number_in_id_field_accepted(cfg, store):
    ids = top_ids(cfg, store)
    res = planner.plan(cfg, gmap(), store, chat=Script([ranking(["2", "1."])]))
    assert not res.fallback and [s.candidate.id for s in res.ranked[:2]] == [ids[1], ids[0]]


def test_invalid_ids_get_one_corrective_retry(cfg, store):
    ids = top_ids(cfg, store)
    chat = Script([ranking(["nope:a:1s", "nope:b:1s"]), ranking(ids)])
    res = planner.plan(cfg, gmap(), store, chat=chat)
    assert not res.fallback and chat.calls == 2
    retry_user = chat.seen[1][1][1]
    assert "not in the list" in retry_user and ids[0] in retry_user and "nope:a:1s" in retry_user
    assert [s.candidate.id for s in res.ranked] == ids


def test_invalid_ids_twice_falls_back(cfg, store):
    chat = Script([ranking(["nope:a:1s"])])
    res = planner.plan(cfg, gmap(), store, chat=chat)
    assert res.fallback and "no valid candidate ids" in res.fallback_reason and chat.calls == 2
    assert [s.candidate.id for s in res.ranked] == top_ids(cfg, store)


def test_bad_cached_ranking_is_dropped(cfg, store, monkeypatch):
    monkeypatch.setenv("FAULTLINE_LLM_CACHE", "1")
    ids = top_ids(cfg, store)
    planner.plan(cfg, gmap(), store, chat=Script([ranking(["nope:a:1s"]), ranking(ids)]))
    chat = Script([ranking(ids)])
    res = planner.plan(cfg, gmap(), store, chat=chat)
    assert not res.fallback and chat.calls == 1


def noisy_map():
    g = gmap()
    for n, k in (("otel-collector", "infra"), ("load-generator", "tooling"), ("flagd-ui", "app"), ("image-provider", "app")):
        g["nodes"][n] = {"stateful": False, "kind": k}
    g["edges"] += [{"source": n, "target": "frontend"} for n in ("otel-collector", "load-generator", "flagd-ui", "image-provider")]
    return g


def test_fallback_hypothesis_ignores_telemetry_callers(cfg):
    g = noisy_map()
    c = next(c for c in planner.generate_candidates(cfg, g) if c.id == "delay:frontend:300ms")
    h = planner.template_hypothesis(g, c)
    for n in ("otel-collector", "load-generator", "flagd-ui", "image-provider"):
        assert n not in h
    assert "add_to_cart" in h and "home" in h
    blast = planner.blast_radius(g, "frontend")
    assert not {"otel-collector", "load-generator", "flagd-ui", "image-provider"} & set(blast)
    assert "journey:home" in blast


def test_fallback_hypothesis_keeps_journey_callers(cfg):
    g = noisy_map()
    g["journeys"]["home"]["services"].append("image-provider")
    assert "image-provider" in planner.journey_callers(g, "frontend")
    c = next(c for c in planner.generate_candidates(cfg, g) if c.id == "crash:cart:20s")
    assert "frontend" in planner.template_hypothesis(g, c)


def test_fallback_hypothesis_without_journey(cfg):
    g = noisy_map()
    c = next(c for c in planner.generate_candidates(cfg, g) if c.id == "delay:quote:50ms")
    h = planner.template_hypothesis(g, c)
    assert "no user journey depends" in h and "none" not in h


# llm 400 retry

SIMPLE = {"system": "short"}


class Out(llm.BaseModel):
    v: int


def call(store, chat, simple=SIMPLE, **kw):
    return llm.call_structured("fixer", "m", Out, "sys", "usr", store=store, chat=chat, simple=simple, **kw)


def test_400_json_error_retries_with_simple_prompt_and_traces_both(store):
    chat = Script([Err400(), json.dumps({"v": 1})])
    out, meta = call(store, chat)
    assert out.v == 1 and chat.calls == 2 and meta["retried_simple"] and not meta["fallback"]
    assert chat.seen[0][0][1] == "sys" and chat.seen[1][0][1] == "short"
    rows = store.traces()
    assert len(rows) == 2 and rows[0]["fallback"] == 1 and "Failed to generate JSON" in rows[0]["error"]
    assert rows[1]["fallback"] == 0 and rows[1]["input_summary"].endswith("(simple retry)")


def test_400_twice_gives_up_after_two_calls(store):
    chat = Script([Err400()])
    out, meta = call(store, chat)
    assert out is None and chat.calls == 2 and meta["fallback"]
    assert len(store.traces()) == 2


def test_other_400_does_not_retry(store):
    chat = Script([Err400("bad model")])
    out, _ = call(store, chat)
    assert out is None and chat.calls == 1


def test_invalid_output_uses_simple_prompt_as_the_retry(store):
    chat = Script(["not json", json.dumps({"v": 2})])
    out, meta = call(store, chat)
    assert out.v == 2 and chat.calls == 2 and len(chat.seen[1]) == 2


def test_no_simple_prompt_keeps_old_behaviour(store):
    chat = Script([Err400()])
    out, _ = call(store, chat, simple=None)
    assert out is None and chat.calls == 1


def test_fixer_retries_without_compose_override(cfg, store):
    run, rounds, an = sample_analysis(cfg, store)
    ws = [analyst.Weakness(rank=1, title="a", severity="high", services=["cart"], explanation="x", related_experiment_ids=["e1"])]
    good = json.dumps({"fixes": [{"weakness_rank": 1, "fix_title": "t", "fix_type": "config", "steps": "do it", "expected_effect": "e"}]})
    chat = Script([Err400(), good])
    res = fixer.fix(cfg, store, an, gmap(), ws, chat=chat)
    assert not res.fallback and res.llm_fixes == 1 and res.fixes[0].compose_override is None and res.fixes[0].source == "llm"
    assert "compose_override" not in chat.seen[1][0][1] and "compose_override" in chat.seen[0][0][1]


def test_fixer_falls_back_when_both_attempts_fail(cfg, store):
    run, rounds, an = sample_analysis(cfg, store)
    ws = [analyst.Weakness(rank=1, title="a", severity="high", services=["cart"], explanation="x", related_experiment_ids=["e1"])]
    res = fixer.fix(cfg, store, an, gmap(), ws, chat=Script([Err400()]))
    assert res.fallback and res.fixes[0].source == "fallback"


def test_analyst_retries_simple_schema(cfg, store):
    run, rounds, an = sample_analysis(cfg, store)
    good = json.dumps({"weaknesses": [{"rank": 1, "title": "cart single point", "severity": "high", "services": ["cart"], "explanation": "x",
                                       "evidence": "y", "related_experiment_ids": ["e1"]}], "executive_summary": "s"})
    chat = Script([Err400(), good])
    res = analyst.analyze(cfg, store, an, {"cart", "quote"}, chat=chat)
    assert not res.fallback and chat.calls == 2 and res.weaknesses[0].title == "cart single point"


# analyst recovery floor

def rec_weakness(ids, title="Slow recovery from service crashes"):
    return json.dumps({"weaknesses": [
        {"rank": 1, "title": title, "severity": "medium", "services": ["cart"], "explanation": "x", "evidence": "y", "related_experiment_ids": ids},
        {"rank": 2, "title": "cart is a single point", "severity": "high", "services": ["cart"], "explanation": "x", "evidence": "y", "related_experiment_ids": ["e1"]},
    ], "executive_summary": "s"})


def floor_analysis(cfg, store, rec):
    seed_baseline(store, cfg)
    add_exp(store, "e1", "cart", impact={"home": jr(), "add_to_cart": jr(err=0.6, p95=2000.0)}, recovery=rec)
    run = add_run(store, [{"target": "cart", "experiment_id": "e1"}])
    return analyzer.analyze(cfg, store, gmap(), run, runs.rounds(store, "run-test"))


def test_recovery_weakness_at_floor_is_dropped(cfg, store):
    an = floor_analysis(cfg, store, 5.2)
    res = analyst.analyze(cfg, store, an, {"cart"}, chat=Script([rec_weakness(["e1"])]))
    assert [w.title for w in res.weaknesses] == ["cart is a single point"] and res.dropped == 1 and res.weaknesses[0].rank == 1


def test_recovery_weakness_kept_above_floor_or_never(cfg, store):
    for rec in (30.0, None):
        s = type(store)(store.path + str(rec))
        an = floor_analysis(cfg, s, rec)
        res = analyst.analyze(cfg, s, an, {"cart"}, chat=Script([rec_weakness(["e1"])]))
        assert len(res.weaknesses) == 2 and res.dropped == 0
        s.close()


def test_prompt_marks_floor(cfg, store):
    an = floor_analysis(cfg, store, 5.2)
    assert "floor: 6s" in analyst.compact(an, floor_s=6) and "at floor, normal" in analyst.compact(an, floor_s=6)


def test_fallback_has_no_recovery_driven_weakness(cfg, store):
    seed_baseline(store, cfg)
    add_exp(store, "e1", "cart", impact=healthy_impact(), recovery=5.5)
    add_exp(store, "e2", "quote", impact={"home": jr(), "add_to_cart": jr(err=0.6, p95=2000.0)}, recovery=5.0)
    run = add_run(store, [{"target": "cart", "experiment_id": "e1"}, {"target": "quote", "experiment_id": "e2"}])
    an = analyzer.analyze(cfg, store, gmap(), run, runs.rounds(store, "run-test"))
    ws = analyst.fallback_weaknesses(cfg, an)
    assert [w.services for w in ws] == [["quote"]]
    assert "recover" not in (ws[0].title + ws[0].explanation).lower().replace("recovered at the measurement floor", "")


def healthy_impact():
    return {"home": jr(), "add_to_cart": jr()}


def test_fallback_threshold_ignores_floor_recovery(cfg):
    a = {"penalty": 10.5, "never_recovered": False, "recovery_s": 6.0, "recovery_norm": 0.1}
    assert analyst.non_recovery_penalty(cfg, a) == pytest.approx(8.5)
    a["never_recovered"] = True
    assert analyst.non_recovery_penalty(cfg, a) == 10.5


# not measured recovery

def test_not_measured_recovery_is_not_never_recovered(cfg, store):
    seed_baseline(store, cfg)
    add_exp(store, "e1", "cart", impact=healthy_impact(), recovery=None)
    store.conn.execute("UPDATE experiments SET recovery_measured = 0, state = 'ABORTED', abort_source = 'operator'")
    run = add_run(store, [{"target": "cart", "experiment_id": "e1"}])
    a = analyzer.analyze(cfg, store, gmap(), run, runs.rounds(store, "run-test"))["experiments"][0]
    assert a["recovery_measured"] is False and a["never_recovered"] is False and a["recovery_norm"] == 0 and a["penalty"] == 0


def test_never_recovered_still_scores_one_when_measured(cfg, store):
    seed_baseline(store, cfg)
    add_exp(store, "e1", "cart", impact=healthy_impact(), recovery=None)
    store.conn.execute("UPDATE experiments SET recovery_measured = 1")
    run = add_run(store, [{"target": "cart", "experiment_id": "e1"}])
    a = analyzer.analyze(cfg, store, gmap(), run, runs.rounds(store, "run-test"))["experiments"][0]
    assert a["never_recovered"] is True and a["recovery_norm"] == 1.0 and a["recovery_measured"] is True


def test_legacy_operator_abort_without_flag_is_not_measured(cfg):
    assert analyzer.recovery_measured({"recovery_seconds": None, "abort_source": "operator"}) is False
    assert analyzer.recovery_measured({"recovery_seconds": None, "abort_source": "watchdog"}) is True
    assert analyzer.recovery_measured({"recovery_seconds": 3.0, "abort_source": "operator"}) is True


def test_report_notes_unmeasured_recovery(cfg, store):
    from faultline import report
    seed_baseline(store, cfg)
    add_exp(store, "e1", "cart", impact=healthy_impact(), recovery=None)
    store.conn.execute("UPDATE experiments SET recovery_measured = 0, state = 'ABORTED', abort_source = 'operator'")
    run = add_run(store, [{"target": "cart", "experiment_id": "e1"}])
    an = analyzer.analyze(cfg, store, gmap(), run, runs.rounds(store, "run-test"))
    ar = analyst.analyze(cfg, store, an, {"cart"})
    fr = fixer.fix(cfg, store, an, gmap(), ar.weaknesses)
    d = report.build(cfg, store, run, runs.rounds(store, "run-test"), an, ar, fr, time.time() - 5)
    text = report.render_md(report.blocks(cfg, d))
    assert d["notes"]["recovery_not_measured"] == ["e1"]
    assert "Recovery was not measured for e1" in text and "not measured" in report.fmt_rec(an["experiments"][0])


def test_operator_abort_ignored_by_memory(cfg, store):
    add_exp(store, "e1", "cart", state="ABORTED", reason="operator interrupt (Ctrl+C / signal)", recovery=None)
    store.conn.execute("UPDATE experiments SET abort_source = 'operator', recovery_measured = 0")
    assert memory.known_limits(cfg, store) == []


def test_ctrl_c_during_observing_records_not_measured(monkeypatch, ecfg, store):
    sim = Sim()
    stop = asyncio.Event()
    e = exp_go(monkeypatch, ecfg, store, SimDriver(sim, on_apply=lambda en: stop.set()), sim, duration=3, stop=stop)
    assert e["state"] == "ABORTED" and e["abort_source"] == "operator"
    assert e["recovery_seconds"] is None and e["recovery_measured"] == 0


def test_normal_experiment_records_measured(monkeypatch, ecfg, store):
    sim = Sim()
    e = exp_go(monkeypatch, ecfg, store, SimDriver(sim), sim, duration=1)
    assert e["state"] == "COMPLETED" and e["recovery_measured"] == 1 and e["recovery_seconds"] is not None


def test_never_recovered_when_cap_reached_is_measured(monkeypatch, ecfg, store):
    sim = Sim()
    sim.bad = True

    class Stuck(SimDriver):
        def revert(self, entry):
            super().revert(entry)
            self.sim.faulted = True

    e = exp_go(monkeypatch, ecfg, store, Stuck(sim), sim, duration=1)
    assert e["recovery_seconds"] is None and e["recovery_measured"] == 1


# clean

def test_clean_normalises_arrows_and_symbols():
    t = "a \u2192 b \u2264 3 \u00d7 \u2026 \u201cq\u201d \u2013 x\u00a0y \u2022 z \u2248 \u2265"
    out = analyzer.clean(t)
    assert out.isascii()
    assert "->" in out and "<=" in out and ">=" in out and "..." in out and '"q"' in out


def test_safety_reasons_are_ascii(cfg, store):
    out = safety.ReviewOut.model_validate({"decision": "approve", "reason": "p95 \u2192 high \u2014 fine \u2264 limit"})
    assert out.reason.isascii()
    rec = {"decision": "approve", "reason": "x \u2192 y", "fallback": 0, "final": {}, "original": {}}
    assert safety.describe_review(rec).isascii()
