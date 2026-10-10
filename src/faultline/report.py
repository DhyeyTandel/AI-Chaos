import html
import json
import time
from pathlib import Path

from . import analyzer

MARKER_INPUTS = ("ranking validation", "weakness validation", "fix validation")
AGENTS = ("planner", "safety", "analyst", "fixer")

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
    return "never" if a["never_recovered"] else "-" if a["recovery_s"] is None else f"{a['recovery_s']:.1f}s"


def fmt_ts(t) -> str:
    return "-" if not t else time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(t))


def ai_usage(store, run: dict, since: float, results: dict) -> list[dict]:
    now = time.time()
    out = []
    for agent in AGENTS:
        lo, hi = (run["started_at"], run.get("ended_at") or now) if agent in ("planner", "safety") else (since, now)
        rows = [dict(r) for r in store.conn.execute("SELECT * FROM agent_trace WHERE agent = ? AND ts >= ? AND ts <= ? ORDER BY id", (agent, lo, hi + 1))]
        real = [r for r in rows if r["input_summary"] not in MARKER_INPUTS]
        groq = [r for r in real if not r["fallback"]]
        fb = [r for r in rows if r["fallback"]]
        models = sorted({r["model"] for r in groq if r["model"]})
        res = results.get(agent)
        if res is not None:
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
        "A baseline run fixes per-journey error rate and p95 latency and derives safety thresholds from them. A watchdog aborts any experiment that breaches a threshold or loses sight of the prober.",
        "Each experiment injects one fault (container crash, added network delay or packet loss), measures every journey during the fault window (up to the abort for aborted runs), reverts the fault, and measures how long the journeys take to return inside the baseline band.",
        "Impact, breadth, recovery and the penalty are computed deterministically from those measurements. The hypothesis verdict compares the journeys the planner predicted would degrade with those that actually did: CONFIRMED when equal, PARTIAL when they overlap, REFUTED when they do not.",
        f"A hidden dependency is a journey that degraded by at least {cfg.score.hidden_min_impact} although neither the hypothesis nor the dependency map expected it.",
        "The analyst agent ranks weaknesses and the fixer agent proposes fixes from the measured results only; their output is validated against known experiments, services and an allowlist of compose keys. When Groq is unavailable or its output is invalid, rule-based fallbacks produce the same sections. Fixes are proposals and are never applied.",
    ]


def formula_blocks(cfg) -> list:
    s = cfg.score
    return [
        ("code", f"penalty = 100 x ({s.impact_weight} x impact + {s.breadth_weight} x breadth + {s.recovery_weight} x recovery)\nservice score = 100 - worst penalty observed on that service\noverall = mean of service scores weighted by journey dependency"),
        ("ul", [
            f"impact: worst journey degradation in the fault window. Per journey, max(error part, latency part). Error part = (error rate - baseline error rate) / {s.err_full}, clamped to 0..1. Latency part = ln(p95 / max(baseline p95, {s.p95_base_floor_ms} ms)) / ln({s.p95_full_ratio:g}), clamped to 0..1.",
            f"breadth: affected journeys / all journeys. A journey is affected when its degradation is at least {s.affected_margin} or it breaches the baseline threshold.",
            f"recovery: min(1, recovery seconds / {s.recovery_cap_s}); a fault that never recovered scores 1. Recovery is measured from the revert until the journeys are back inside the baseline band.",
            "overall weights: each service counts once per journey it sits on in the dependency map (at least 1); a service seen degrading more journeys than the map lists uses the observed count. Untested services are excluded and listed.",
            "rating: 80 and above good, 60 to 79 fair, below 60 poor.",
        ]),
    ]


def rating(score) -> str:
    return "n/a" if score is None else "good" if score >= 80 else "fair" if score >= 60 else "poor"


def build(cfg, store, run: dict, rounds: list[dict], analysis: dict, analyst_res, fixer_res, since: float) -> dict:
    fixes = {f.weakness_rank: f for f in fixer_res.fixes}
    weaknesses = [{**w.model_dump(), "fix": fixes[w.rank].model_dump() if w.rank in fixes else None} for w in analyst_res.weaknesses]
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
        "notes": {"analyst_dropped": analyst_res.dropped, "fixer_filled_by_fallback": fixer_res.filled},
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
    b.append(("p", "Fixes are proposals only. Nothing was applied to the running system."))
    if not d["weaknesses"]:
        b.append(("p", "No weaknesses reached the reporting threshold."))
    for w in d["weaknesses"]:
        b.append(("h", 3, f"{w['rank']}. {w['title']} [{w['severity'].upper()}]", f"sev-{w['severity']}"))
        b.append(("p", f"Services: {', '.join(w['services']) or '-'}. Experiments: {', '.join(w['related_experiment_ids'])}. Source: {w['source']}."))
        b += [("p", w["explanation"]), ("p", "Evidence: " + w["evidence"])]
        f = w.get("fix")
        if f:
            b.append(("p", f"Proposed fix ({f['fix_type']}, {f['source']}): {f['fix_title']}. {f['steps']}"))
            if f["compose_override"]:
                b += [("p", "Compose override (not applied):"), ("code", f["compose_override"].rstrip())]
            elif f.get("override_dropped"):
                b.append(("p", f"Compose override dropped by validation: {f['override_dropped']}."))
            if f["expected_effect"]:
                b.append(("p", "Expected effect: " + f["expected_effect"]))
    run_exps = [a for a in d["experiments"] if a["scope"] == "run"]
    hist = [a for a in d["experiments"] if a["scope"] == "history"]
    b.append(("h", 2, "Experiments in this run", ""))
    cut = lambda s, n: (s or "-") if len(s or "") <= n else s[: n - 3] + "..."
    b.append(("table", ["Round", "Tier", "Fault", "Target", "AI reason", "Hypothesis", "Verdict", "Degraded journeys", "Impact", "Breadth", "Recovery", "Penalty", "Outcome"],
              [[a["round"], a["tier"], a["label"], a["target"], cut(a["reason"], 140), cut(a["hypothesis"], 140), a["verdict"],
                ", ".join(a["affected_journeys"]) or "none", f"{a['impact']:.2f}", f"{a['breadth']:.2f}", fmt_rec(a), f"{a['penalty']:.0f}",
                a["outcome"] + (f" ({cut(a['abort_reason'], 60)})" if a["abort_reason"] else "")] for a in run_exps]))
    if hist:
        b.append(("h", 2, "Earlier experiments in the database", ""))
        b.append(("p", "These ran before this run and count toward service scores and hidden dependency findings."))
        b.append(("table", ["Experiment", "Fault", "Target", "Degraded journeys", "Impact", "Breadth", "Recovery", "Penalty", "Outcome"],
                  [[a["experiment_id"], a["label"], a["target"], ", ".join(a["affected_journeys"]) or "none", f"{a['impact']:.2f}", f"{a['breadth']:.2f}",
                    fmt_rec(a), f"{a['penalty']:.0f}", a["outcome"]] for a in hist]))
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
    n = d["notes"]
    b.append(("p", f"Analyst weaknesses dropped by validation: {n['analyst_dropped']}. Fixes filled by the rule-based catalog: {n['fixer_filled_by_fallback']}."))
    return b


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
