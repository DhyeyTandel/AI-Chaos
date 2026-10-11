import html
import json
import time
from pathlib import Path

from . import analyzer, fixes as fixes_mod, memory
from .agents import injection as injection_agent, safety

MARKER_INPUTS = ("ranking validation", "weakness validation", "fix validation")
AGENTS = ("planner", "safety", "injection", "recovery", "analyst", "fixer")
RUN_WINDOW = ("planner", "safety", "injection", "recovery")

CSS = """
:root{--bg:#fbfaf8;--fg:#1d1b19;--mut:#6a645d;--line:#ddd7cf;--card:#fff;--accent:#c2410c;--crit:#b91c1c;--high:#c2410c;--med:#a16207;--low:#4d7c0f}
@media (prefers-color-scheme:dark){:root{--bg:#161412;--fg:#ece8e2;--mut:#a39b91;--line:#38332e;--card:#1f1c19;--accent:#fb923c;--crit:#f87171;--high:#fb923c;--med:#facc15;--low:#a3e635}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:1100px;margin:0 auto;padding:32px 16px 64px}
h1{font-size:28px;margin:0 0 8px}h2{font-size:20px;margin:40px 0 12px;padding-bottom:6px;border-bottom:1px solid var(--line)}h3{font-size:16px;margin:24px 0 6px}
p{margin:8px 0}ul{margin:8px 0;padding-left:22px}
.score{font-size:56px;font-weight:700;color:var(--accent);line-height:1.1}.cap{color:var(--mut);margin-top:0}
.wrap{overflow-x:auto;border:1px solid var(--line);border-radius:8px;background:var(--card)}
table{border-collapse:collapse;width:100%;font-size:13px}th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--mut);font-weight:600;white-space:nowrap}tr:last-child td{border-bottom:0}
pre{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px;overflow-x:auto;font-size:13px}
.sev-critical{color:var(--crit)}.sev-high{color:var(--high)}.sev-medium{color:var(--med)}.sev-low{color:var(--low)}
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
"""


def fmt_rec(a: dict) -> str:
    if not a["injected"]:
        return "-"
    if not a.get("recovery_measured", True):
        return "not measured"
    if a["never_recovered"]:
        return "never" + (" (agent escalated)" if a.get("recovery_agent") == "escalated" else "")
    if a["recovery_s"] is None:
        return "-"
    return f"{a['recovery_s']:.1f}s" + (" (after agent)" if a.get("initial_recovery_failed") else "")


def fmt_ts(t) -> str:
    return "-" if not t else time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(t))


def ai_usage(store, run: dict, since: float, results: dict) -> list[dict]:
    now = time.time()
    out = []
    for agent in AGENTS:
        lo, hi = (run["started_at"], run.get("ended_at") or now) if agent in RUN_WINDOW else (since, now)
        rows = [dict(r) for r in store.conn.execute("SELECT * FROM agent_trace WHERE agent = ? AND ts >= ? AND ts <= ? ORDER BY id", (agent, lo, hi + 1))]
        det = [r for r in rows if r["model"] == "deterministic"]
        rows = [r for r in rows if r["model"] != "deterministic"]
        real = [r for r in rows if r["input_summary"] not in MARKER_INPUTS]
        groq = [r for r in real if not r["fallback"]]
        fb = [r for r in rows if r["fallback"]]
        models = sorted({r["model"] for r in groq if r["model"]})
        res = results.get(agent)
        if agent == "injection":
            path = f"deterministic ({len(det)} injections checked, no LLM)" if det else "not run"
        elif agent == "recovery" and not rows:
            path = f"deterministic (no LLM call; {len(det)} records)" if det else "not run"
        elif res is not None:
            path = f"fallback ({res['reason']})" if res["fallback"] else f"groq {', '.join(models) or res['model']}"
        elif not rows:
            path = "not run"
        else:
            path = ("groq " + ", ".join(models)) if groq and not fb else "fallback" if not groq else "mixed: groq " + ", ".join(models) + f" and {len(fb)} fallback"
        out.append({
            "agent": agent, "path": path, "calls": len(real), "groq_calls": len(groq), "fallbacks": len(fb),
            "cached": sum(1 for r in groq if r["error"] == "cache"),
            "prompt_tokens": sum(r["prompt_tokens"] or 0 for r in rows), "completion_tokens": sum(r["completion_tokens"] or 0 for r in rows),
        })
    return out


def methodology_text(cfg) -> list[str]:
    return [
        "An independent synthetic prober exercises four user journeys (home, product, cart, checkout) through the front proxy every second. It is the ground truth and does not depend on the application's own telemetry.",
        "A baseline run fixes per-journey error rate and p95 latency and derives safety thresholds from them. A watchdog aborts any experiment that breaches a threshold or loses sight of the prober. Before injection, the safety agent reviews the plan against the baseline limits, recent outcomes and the learned limits, and may approve it, reduce its intensity or veto it; it can never raise an intensity. The planner remembers, across runs, the lowest intensity per target and fault kind that the watchdog or safety agent aborted, and does not propose that intensity or a higher one again within the lookback window. An advisory safety agent re-checks the live measurements every few seconds and may abort earlier; it can never cancel a watchdog abort, extend a fault or change a threshold.",
        "The injection agent is deterministic: it applies the approved plan through the ledger and then verifies that each part is really in effect, reverting only its own entries if not. The recovery agent (a small LLM) runs only after the deterministic revert and recovery measurement, and only if the system did not return to baseline; it can restart services from a fixed, guarded menu or escalate to the operator, and it is never used when recovery succeeded.",
        "Each experiment injects one fault (container crash, added network delay, packet loss or a traffic spike), measures every journey during the fault window (up to the abort for aborted runs), reverts the fault, and measures how long the journeys take to return inside the baseline band.",
        f"A declared combined failure injects {cfg.combined.max_parts} faults together as one experiment (one ledger entry per part, one watchdog, one safety review). Every journey limit is tightened for it: p95 limit x{cfg.combined.p95_factor:g}, error-rate limit x{cfg.combined.error_factor:g}. It is scored once, as a whole.",
        "Impact, breadth, recovery and the penalty are computed deterministically from those measurements. The hypothesis verdict compares the journeys the planner predicted would degrade with those that actually did: CONFIRMED when equal, PARTIAL when they overlap, REFUTED when they do not.",
        f"A hidden dependency is a journey that degraded by at least {cfg.score.hidden_min_impact} although neither the hypothesis nor the dependency map expected it.",
        "The analyst agent ranks weaknesses and the fixer agent proposes fixes from the measured results only; their output is validated against known experiments, services and an allowlist of compose keys. When Groq is unavailable or its output is invalid, rule-based fallbacks produce the same sections. Fixes are proposals; only restart-policy and raised memory or CPU limit fixes on one allowlisted service can be applied, by `faultline fix`, and are kept only if a re-run of the same experiment improves the penalty.",
    ]


def formula_blocks(cfg) -> list:
    s = cfg.score
    return [
        ("code", f"penalty = 100 x ({s.impact_weight} x impact + {s.breadth_weight} x breadth + {s.recovery_weight} x recovery)\nservice score = 100 - worst penalty observed on that service\noverall = mean of service scores weighted by journey dependency"),
        ("ul", [
            f"impact: worst journey degradation in the fault window. Per journey, max(error part, latency part). Error part = (error rate - baseline error rate) / {s.err_full}, clamped to 0..1. Latency part = ln(p95 / max(baseline p95, {s.p95_base_floor_ms} ms)) / ln({s.p95_full_ratio:g}), clamped to 0..1.",
            f"breadth: affected journeys / all journeys. A journey is affected when its degradation is at least {s.affected_margin} or it breaches the baseline threshold.",
            f"recovery: min(1, recovery seconds / {s.recovery_cap_s}); a fault that never recovered scores 1, and an experiment stopped by the operator before recovery was measured contributes 0. Recovery is measured from the revert until the journeys are back inside the baseline band.",
            "overall weights: each service counts once per journey it sits on in the dependency map (at least 1); a service seen degrading more journeys than the map lists uses the observed count. Untested services are excluded and listed.",
            "rating: 80 and above good, 60 to 79 fair, below 60 poor.",
        ]),
    ]


def rating(score) -> str:
    return "n/a" if score is None else "good" if score >= 80 else "fair" if score >= 60 else "poor"


def learned_limits(cfg, store, run: dict, analysis: dict) -> dict:
    mem = run.get("memory") or {}
    new = [
        {"experiment_id": a["experiment_id"], "target": a["target"], "fault": a["label"], "source": a["abort_source"]}
        for a in analysis["experiments"]
        if a["scope"] == "run" and a["outcome"] == "ABORTED" and a["abort_source"] in memory.SAFETY_SOURCES
    ]
    return {
        "enabled": memory.enabled(cfg), "lookback_days": cfg.planner.memory.lookback_days,
        "limits": mem.get("limits") or [], "exclusions": mem.get("exclusions") or [], "new": new,
        "combo_limits": memory.combo_limits(cfg, store),
    }


def preflight_data(rounds: list[dict]) -> dict:
    reviews = [r["preflight"] for r in rounds if r.get("preflight") and r["preflight"].get("decision")]
    vetoed = [{"round": r["round"], **v} for r in rounds for v in ((r.get("preflight") or {}).get("vetoed") or [])]
    return {"reviews": reviews, "vetoed": vetoed}


def fix_data(store, analysis: dict) -> list[dict]:
    out = []
    for f in fixes_mod.report_rows(store, analysis):
        d = f.get("details") or {}
        out.append({
            "id": f["id"], "service": f["target"], "change": fixes_mod.describe_change(f["change"]), "skipped": f["change"].get("skipped") or [],
            "state": f["state"], "verdict": f["verdict"] or "not verified", "applied_at": f["applied_at"], "reverted_at": f["reverted_at"],
            "weakness_rank": f["weakness_rank"], "before_penalty": f["before_penalty"], "after_penalty": f["after_penalty"],
            "reason": d.get("reason"), "pairs": d.get("pairs") or [], "original": {k: v for k, v in f["original"].items() if k != "container"},
            "before_experiments": f["before_experiment_ids"], "after": f["after"], "error": f["error"],
        })
    return out


def build(cfg, store, run: dict, rounds: list[dict], analysis: dict, analyst_res, fixer_res, since: float) -> dict:
    fixes = {f.weakness_rank: f for f in fixer_res.fixes}
    weaknesses = [
        {**w.model_dump(), "fix": fixes[w.rank].model_dump() if w.rank in fixes else None,
         "applicability": fixes_mod.applicability_text(cfg, fixes[w.rank]) if w.rank in fixes else None}
        for w in analyst_res.weaknesses
    ]
    results = {
        "analyst": {"fallback": analyst_res.fallback, "reason": analyst_res.fallback_reason, "model": analyst_res.model},
        "fixer": {"fallback": fixer_res.fallback, "reason": fixer_res.fallback_reason, "model": fixer_res.model},
    }
    return analyzer.clean_deep({
        "run": {**{k: run[k] for k in ("run_id", "started_at", "ended_at", "rounds_planned", "rounds_done", "final_tier", "stop_reason")}},
        "generated_at": time.time(), "overall_score": analysis["overall"], "rating": rating(analysis["overall"]),
        "executive_summary": analyst_res.executive_summary, "weaknesses": weaknesses,
        "experiments": analysis["experiments"], "hidden_dependencies": analysis["hidden"],
        "services": analysis["services"], "untested_services": analysis["untested"],
        "score_config": {k: v for k, v in vars(cfg.score).items()}, "formula": analyzer.formula_text(cfg),
        "methodology": methodology_text(cfg), "ai_usage": ai_usage(store, run, since, results),
        "fix_verification": fix_data(store, analysis), "preflight": preflight_data(rounds), "learned_limits": learned_limits(cfg, store, run, analysis),
        "notes": {
            "analyst_dropped": analyst_res.dropped, "fixer_filled_by_fallback": fixer_res.filled,
            "recovery_not_measured": [a["experiment_id"] for a in analysis["experiments"] if a["injected"] and not a["recovery_measured"]],
        },
    })


def blocks(cfg, d: dict) -> list:
    r = d["run"]
    b: list = [("h", 1, "Faultline resilience report", "")]
    b.append(("table", ["Run", "Started", "Ended", "Rounds", "Final tier", "Stop reason"],
              [[r["run_id"], fmt_ts(r["started_at"]), fmt_ts(r["ended_at"]), f"{r['rounds_done']}/{r['rounds_planned']}", r["final_tier"] or "-", r["stop_reason"] or "-"]]))
    sc = d["overall_score"]
    b.append(("score", "n/a" if sc is None else f"{sc:.1f} / 100", f"Overall resilience score ({d['rating']}). Generated {fmt_ts(d['generated_at'])}."))
    b += [("h", 2, "Executive summary", ""), ("p", d["executive_summary"])]
    b.append(("h", 2, "Ranked weaknesses and proposed fixes", ""))
    b.append(("p", "Fixes are proposals. Only a config fix that maps to a restart policy or a raised memory or CPU limit on one allowlisted service can be "
                   "applied, by `faultline fix` after confirmation, and it is then verified by re-running the experiment (see Fix verification). "
                   "Healthcheck, replica, code and architecture changes are propose-only. Nothing is applied by generating this report."))
    if not d["weaknesses"]:
        b.append(("p", "No weaknesses reached the reporting threshold."))
    for w in d["weaknesses"]:
        b.append(("h", 3, f"{w['rank']}. {w['title']} [{w['severity'].upper()}]", f"sev-{w['severity']}"))
        b.append(("p", f"Services: {', '.join(w['services']) or '-'}. Experiments: {', '.join(w['related_experiment_ids'])}. Source: {w['source']}."))
        b += [("p", w["explanation"]), ("p", "Evidence: " + w["evidence"])]
        f = w.get("fix")
        if f:
            b.append(("p", f"Proposed fix ({f['fix_type']}, {f['source']}): {f['fix_title']}. {f['steps']}"))
            if w.get("applicability"):
                b.append(("p", w["applicability"]))
            if f["compose_override"]:
                b += [("p", "Compose override:"), ("code", f["compose_override"].rstrip())]
            elif f.get("override_dropped"):
                b.append(("p", f"Compose override dropped by validation: {f['override_dropped']}."))
            if f["expected_effect"]:
                b.append(("p", "Expected effect: " + f["expected_effect"]))
    b += fix_blocks(d.get("fix_verification") or [])
    run_exps = [a for a in d["experiments"] if a["scope"] == "run"]
    hist = [a for a in d["experiments"] if a["scope"] == "history"]
    b.append(("h", 2, "Experiments in this run", ""))
    cut = lambda s, n: (s or "-") if len(s or "") <= n else s[: n - 3] + "..."
    b.append(("table", ["Round", "Tier", "Fault", "Target", "AI reason", "Hypothesis", "Verdict", "Degraded journeys", "Impact", "Breadth", "Recovery", "Penalty", "Safety review", "Safety agent", "Outcome"],
              [[a["round"], a["tier"], a["label"], a["target"], cut(a["reason"], 140), cut(a["hypothesis"], 140), a["verdict"],
                ", ".join(a["affected_journeys"]) or "none", f"{a['impact']:.2f}", f"{a['breadth']:.2f}", fmt_rec(a), f"{a['penalty']:.0f}", cut(safety.describe_review(a["preflight"]), 120), safety.describe(a["safety_decisions"]),
                a["outcome"] + (f" by {a['abort_source']}" if a["abort_source"] else "") + (f" ({cut(a['abort_reason'], 60)})" if a["abort_reason"] else "")] for a in run_exps]))
    b += agent_blocks(cfg, run_exps)
    combos = [a for a in d["experiments"] if a["combo"]]
    if combos:
        b += combo_blocks(cfg, combos)
    if d["preflight"]["vetoed"]:
        b.append(("p", "Vetoed by the pre-flight safety review (not injected, not counted as aborts): " + "; ".join(
            f"round {v['round']} {v['candidate']} ({cut(v['reason'], 120)})" for v in d["preflight"]["vetoed"])))
    if hist:
        b.append(("h", 2, "Earlier experiments in the database", ""))
        b.append(("p", "These ran before this run and count toward service scores and hidden dependency findings."))
        b.append(("table", ["Experiment", "Fault", "Target", "Degraded journeys", "Impact", "Breadth", "Recovery", "Penalty", "Outcome"],
                  [[a["experiment_id"], a["label"], a["target"], ", ".join(a["affected_journeys"]) or "none", f"{a['impact']:.2f}", f"{a['breadth']:.2f}",
                    fmt_rec(a), f"{a['penalty']:.0f}", a["outcome"]] for a in hist]))
    b += learned_blocks(d["learned_limits"])
    b.append(("h", 2, "Hidden dependencies", ""))
    if d["hidden_dependencies"]:
        b.append(("table", ["Experiment", "Scope", "Fault", "Target", "Unexpected journeys", "Worst degradation", "Map"],
                  [[h["experiment_id"], h["scope"], h["fault"], h["target"], ", ".join(h["unexpected_journeys"]), f"{h['worst_degradation']:.2f}", h["map_note"]] for h in d["hidden_dependencies"]]))
    else:
        b.append(("p", "None found: every degraded journey was predicted or listed in the dependency map."))
    b.append(("h", 2, "Per-service scores", ""))
    b.append(("table", ["Service", "Score", "Worst experiment", "Fault", "Experiments", "Weight"],
              [[t, f"{v['score']:.1f}", v["worst_experiment"], v["worst_label"], v["experiments"], v["weight"]] for t, v in sorted(d["services"].items(), key=lambda kv: kv[1]["score"])]))
    b.append(("p", "Untested services (excluded from the overall score): " + (", ".join(d["untested_services"]) or "none")))
    b += [("h", 2, "Score formula", "")] + formula_blocks(cfg)
    b += [("h", 2, "Methodology", ""), ("ul", d["methodology"])]
    b.append(("h", 2, "AI usage", ""))
    b.append(("table", ["Agent", "Path", "Groq calls", "Fallbacks", "Cached", "Prompt tokens", "Completion tokens"],
              [[u["agent"], u["path"], u["groq_calls"], u["fallbacks"], u["cached"], u["prompt_tokens"], u["completion_tokens"]] for u in d["ai_usage"]]))
    b.append(("p", safety_summary(run_exps)))
    b.append(("p", review_summary(d["preflight"])))
    n = d["notes"]
    b.append(("p", f"Analyst weaknesses dropped by validation: {n['analyst_dropped']}. Fixes filled by the rule-based catalog: {n['fixer_filled_by_fallback']}."))
    if n.get("recovery_not_measured"):
        b.append(("p", "Recovery was not measured for " + ", ".join(n["recovery_not_measured"]) + " (stopped by the operator before recovery could be measured). Their recovery component is excluded from the penalty (counted as 0), not scored as never recovered."))
    return b


def fmt_pen(v) -> str:
    return "-" if v is None else f"{v:.1f}"


def fix_blocks(items: list[dict]) -> list:
    b: list = [("h", 2, "Fix verification", "")]
    if not items:
        return b + [("p", "No fix was applied for the experiments in this report. Run `faultline fix --run <run id>` to apply the top auto-applicable fix and re-run its experiments.")]
    b.append(("p", "Each applied fix was made through the Docker API on the running container, the same experiment was re-run with the same parameters, and the mean penalty "
                   "before and after was compared. The before data is unchanged; after-fix experiments are listed separately and are not counted in the scores above. "
                   "A fix is kept only when the verdict is IMPROVED (default); otherwise the original values are restored."))
    b.append(("table", ["Fix", "Weakness", "Service and change", "Before penalty", "After penalty", "Verdict", "State"],
              [[f"#{x['id']}", x["weakness_rank"] or "-", x["change"], fmt_pen(x["before_penalty"]), fmt_pen(x["after_penalty"]), x["verdict"],
                x["state"] + (" (kept)" if x["state"] == fixes_mod.APPLIED else "")] for x in items]))
    for x in items:
        if x["reason"]:
            b.append(("p", f"Fix #{x['id']} could not be verified: {x['reason']}."))
        if x["skipped"]:
            b.append(("p", f"Fix #{x['id']} propose-only part (not applied): {', '.join(x['skipped'])}."))
        if x["error"]:
            b.append(("p", f"Fix #{x['id']} note: {x['error']}"))
        rows = []
        for pr in x["pairs"]:
            for j, v in pr["journeys"].items():
                pct = lambda e: "-" if e is None else f"{100 * e:.1f}"
                ms = lambda m: "-" if m is None else f"{m:.0f}"
                rows.append([f"{pr['before']} -> {pr['after']}", j, ms(v["p95_before"]), ms(v["p95_after"]), pct(v["err_before"]), pct(v["err_after"])])
        if rows:
            b += [("p", f"Fix #{x['id']} journey metrics during the fault window, before and after:"),
                  ("table", ["Experiments (before -> after)", "Journey", "p95 ms before", "p95 ms after", "err % before", "err % after"], rows)]
        if x["after"]:
            b += [("p", f"Fix #{x['id']} after-fix experiments:"),
                  ("table", ["Experiment", "Fault", "Target", "Degraded journeys", "Impact", "Breadth", "Recovery", "Penalty", "Outcome"],
                   [[a["experiment_id"], a["label"], a["target"], ", ".join(a["affected_journeys"]) or "none", f"{a['impact']:.2f}", f"{a['breadth']:.2f}",
                     fmt_rec(a), f"{a['penalty']:.0f}", a["outcome"]] for a in x["after"]])]
    return b


def agent_blocks(cfg, exps: list[dict]) -> list:
    b: list = [("h", 2, "Injection and recovery agents", "")]
    inj = [a for a in exps if a.get("injection")]
    b.append(("p", "Injection agent (deterministic, no LLM): after the approved plan is applied, it checks that every part is in effect (netem present in the target's network namespace "
                   f"with its sidecar running, crashed container not running, Locust users at {cfg.injection.spike_fraction:.0%} or more of the target). "
                   "An unverified injection reverts only that experiment's own ledger entries and aborts it with source inject."))
    if inj:
        b.append(("table", ["Experiment", "Fault", "Verified parts"],
                  [[a["experiment_id"], a["label"], injection_agent.describe(a["injection"]).removeprefix("Injection agent: ")] for a in inj]))
    b.append(("p", "Recovery agent: runs only after the deterministic revert and recovery measurement. When the journeys returned to the baseline band it makes no LLM call. "
                   f"When recovery was measured and failed, it diagnoses from the degraded journeys, the revert checks, container state and Docker events, and may take at most {cfg.recovery_agent.max_actions} actions from a fixed menu "
                   "(reapply_revert, restart_target, restart_service, kill_switch, escalate). restart_service is limited to allowlisted application services on a degraded journey, never tooling, observability or a data store that is not the experiment's own target. "
                   "The final measured recovery is used for scoring; the failed first attempt stays listed here."))
    helped = [a for a in exps if a.get("initial_recovery_failed")]
    if not helped:
        b.append(("p", "Every experiment in this run recovered on its own: the recovery agent made no LLM call."))
    for a in helped:
        b.append(("p", f"{a['experiment_id']} ({a['label']} on {a['target']}): did not return to the baseline band within {cfg.recovery.cap_s} s of the revert. "
                       f"Outcome: {a['recovery_agent']}; final recovery {fmt_rec(a)}. {a.get('recovery_note') or ''}"))
        if a["recovery_actions"]:
            b.append(("table", ["Step", "Action", "Service", "Diagnosis", "Reason", "Result"],
                      [[x["step"], x["action"], x["service"] or "-", x["diagnosis"] or "-", x["reason"] or "-", x["result"]] for x in a["recovery_actions"]]))
    return b


def combo_blocks(cfg, combos: list[dict]) -> list:
    c = cfg.combined
    b: list = [("h", 2, "Combined failures", "")]
    b.append(("p", f"Each row is one experiment with several faults applied together. Journey limits were tightened (p95 x{c.p95_factor:g}, error rate x{c.error_factor:g}). "
                   "Impact, breadth and recovery are measured once for the whole combination, and hidden dependencies are attributed to the combination, never to a single part."))
    b.append(("table", ["Experiment", "Combination", "Parts", "Degraded journeys", "Impact", "Breadth", "Recovery", "Penalty", "Outcome"],
              [[a["experiment_id"], a["label"], "; ".join(f"{i}. {p['label']}" for i, p in enumerate(a["parts"], 1)), ", ".join(a["affected_journeys"]) or "none",
                f"{a['impact']:.2f}", f"{a['breadth']:.2f}", fmt_rec(a), f"{a['penalty']:.0f}", a["outcome"]] for a in combos]))
    return b


def learned_blocks(m: dict) -> list:
    b: list = [("h", 2, "Learned limits", "")]
    if not m["enabled"]:
        return b + [("p", "Cross-run planner memory is disabled ([planner.memory] enabled = false).")]
    b.append(("p", f"Lowest fault intensity per target and fault kind that the watchdog or safety agent aborted in the last {m['lookback_days']} days, as known when this run started. The planner does not propose that intensity or a higher one."))
    if m["limits"]:
        b.append(("table", ["Target", "Fault", "Lowest aborted intensity", "Aborted by", "Evidence"],
                  [[x["target"], x["kind"], x["label"], x["source"], x["experiment_id"]] for x in m["limits"]]))
    else:
        b.append(("p", "None known at the start of this run."))
    if m["exclusions"]:
        b += [("p", "Candidates excluded during this run:"), ("table", ["Candidate", "Reason", "Evidence"],
              [[x["candidate"], x["reason"], x["evidence"]] for x in m["exclusions"]])]
    if m["new"]:
        b.append(("p", "New limits learned in this run: " + "; ".join(
            f"{x['fault']}{'' if x['fault'].startswith('combo') else ' on ' + x['target']} aborted by {x['source']} ({x['experiment_id']})" for x in m["new"]) + "."))
    if m.get("combo_limits"):
        b += [("p", "Combined failure limits, keyed by the sorted pair of parts. A combination at or above these levels on every part is not proposed; single-fault limits are unaffected."),
              ("table", ["Pair", "Aborted combination", "Aborted by", "Evidence"],
               [[x["key"], x["label"], x["source"], x["experiment_id"]] for x in m["combo_limits"]])]
    return b


def review_summary(pf: dict) -> str:
    recs = pf["reviews"]
    if not recs and not pf["vetoed"]:
        return "Pre-flight review: none recorded in this run (disabled or no key)."
    s = safety.summarize_reviews(recs)
    return (
        f"Pre-flight review: {s['reviews']} reviews of executed experiments (approve {s['approve']}, reduce {s['reduce']}, "
        f"fallback approvals {s['fallback']}) and {len(pf['vetoed'])} vetoes that skipped a candidate. "
        "A veto counts as a planner attempt, not as an abort. A failed review approves, and the validator and watchdog still apply."
    )


def safety_summary(exps: list[dict]) -> str:
    checks = [c for a in exps for c in a["safety_decisions"]]
    if not checks:
        return "Safety agent: no checks recorded in this run (disabled, no key, or no experiment reached observation)."
    s = safety.summarize(checks)
    by = lambda src: sum(1 for a in exps if a["outcome"] == "ABORTED" and a["abort_source"] == src)
    return (
        f"Safety agent: {s['checks']} checks across {sum(1 for a in exps if a['safety_decisions'])} experiments "
        f"(continue {s['continue']}, abort {s['abort']}, ignored {s['ignored']}). Aborts by watchdog: {by('watchdog')}, "
        f"by safety agent: {by('safety_agent')}. Ignored checks (timeout, rate limit, invalid output or budget) leave the watchdog verdict standing."
    )


def md_cell(v) -> str:
    return str(v).replace("|", "/").replace("\n", " ")


def render_md(bl: list) -> str:
    out = []
    for blk in bl:
        k = blk[0]
        if k == "h":
            out.append("#" * blk[1] + " " + blk[2])
        elif k == "p":
            out.append(blk[1])
        elif k == "score":
            out.append(f"**{blk[1]}**  \n{blk[2]}")
        elif k == "ul":
            out.append("\n".join(f"- {x}" for x in blk[1]))
        elif k == "code":
            out.append("```\n" + blk[1] + "\n```")
        elif k == "table":
            rows = ["| " + " | ".join(blk[1]) + " |", "|" + "---|" * len(blk[1])]
            rows += ["| " + " | ".join(md_cell(c) for c in r) + " |" for r in blk[2]]
            out.append("\n".join(rows))
    return "\n\n".join(out) + "\n"


def render_html(bl: list, title: str) -> str:
    e = html.escape
    out = []
    for blk in bl:
        k = blk[0]
        if k == "h":
            cls = f' class="{blk[3]}"' if blk[3] else ""
            out.append(f"<h{blk[1]}{cls}>{e(blk[2])}</h{blk[1]}>")
        elif k == "p":
            out.append(f"<p>{e(blk[1])}</p>")
        elif k == "score":
            out.append(f'<div class="score">{e(blk[1])}</div><p class="cap">{e(blk[2])}</p>')
        elif k == "ul":
            out.append("<ul>" + "".join(f"<li>{e(x)}</li>" for x in blk[1]) + "</ul>")
        elif k == "code":
            out.append(f"<pre><code>{e(blk[1])}</code></pre>")
        elif k == "table":
            head = "".join(f"<th>{e(h)}</th>" for h in blk[1])
            body = "".join("<tr>" + "".join(f"<td>{e(str(c))}</td>" for c in r) + "</tr>" for r in blk[2])
            out.append(f'<div class="wrap"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>')
    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<meta name="color-scheme" content="light dark"><title>{e(title)}</title><style>{CSS}</style></head><body><main>'
        + "\n".join(out) + "</main></body></html>\n"
    )


def write(cfg, d: dict, out_root) -> dict:
    out = Path(out_root) / d["run"]["run_id"]
    out.mkdir(parents=True, exist_ok=True)
    bl = analyzer.clean_deep(blocks(cfg, d))
    paths = {"md": out / "report.md", "html": out / "report.html", "json": out / "report.json"}
    paths["md"].write_text(render_md(bl))
    paths["html"].write_text(render_html(bl, f"Faultline report {d['run']['run_id']}"))
    paths["json"].write_text(json.dumps(d, indent=2))
    return {k: str(v) for k, v in paths.items()}
