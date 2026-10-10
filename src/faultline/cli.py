import asyncio
import json
import os
import signal
import sys
import time
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from . import analyzer, combo as combo_mod, fixes as fixes_mod
from . import baseline as bl
from . import docker_api, experiment as exp_mod, faults, graph as plan_graph, mapper, observer, runs as runs_mod
from .agents import analyst as analyst_mod, fixer as fixer_mod, planner as planner_mod
from .agents import safety as safety_mod
from . import reaper as reaper_mod
from .config import load_config
from .injector import Injector
from .ledger import REVERTED, Ledger
from .prober import JOURNEYS, run_probe
from .stats import journey_stats
from .store import Store, default_db_path

app = typer.Typer(add_completion=False, no_args_is_help=True)
agents_app = typer.Typer(add_completion=False, no_args_is_help=True, help="Agent tools.")
app.add_typer(agents_app, name="agents")
runs_app = typer.Typer(add_completion=False, no_args_is_help=True, help="Stored autonomous runs.")
app.add_typer(runs_app, name="runs")
console = Console(width=int(os.environ["COLUMNS"]) if os.environ.get("COLUMNS", "").isdigit() else None)

DEMO_SAFE = ("quote", "delay", 150)
DEMO_BREACH = ("product-catalog", "crash", 30)


def _data_dir() -> Path:
    return Path(default_db_path()).parent


def _status_table() -> bool:
    infos = docker_api.list_project_containers()
    table = Table("name", "state", "health", "restarts", "oom")
    bad = not infos
    for i in infos:
        problem = i.state != "running" or i.health == "unhealthy"
        bad = bad or problem
        style = "red" if problem else None
        table.add_row(i.name, i.state, i.health, str(i.restarts), "yes" if i.oom_killed else "no", style=style)
    console.print(table)
    if not infos:
        console.print("no containers found for project ai-chaos")
    return bad


@app.command()
def status():
    """Show all ai-chaos containers. Exit 1 if any is not running or unhealthy."""
    raise typer.Exit(1 if _status_table() else 0)


async def _probe_for(store, seconds, timeout=5.0):
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    try:
        loop.add_signal_handler(signal.SIGINT, stop.set)
    except NotImplementedError:
        pass
    await run_probe(store, seconds, timeout=timeout, stop=stop)


@app.command()
def probe(seconds: int = typer.Option(30, "--seconds", "-s", min=1)):
    """Run the journey prober for N seconds and print per-journey stats."""
    store = Store()
    t0 = time.time()
    try:
        asyncio.run(_probe_for(store, seconds))
    except KeyboardInterrupt:
        console.print("interrupted")

    table = Table("journey", "count", "success %", "p50 ms", "p95 ms")
    failed = False
    for j in JOURNEYS:
        s = journey_stats(store, j, since=t0)
        if s.count == 0 or s.success_rate < 1.0:
            failed = True
        table.add_row(
            j,
            str(s.count),
            "-" if s.success_rate is None else f"{s.success_rate * 100:.1f}",
            "-" if s.p50 is None else f"{s.p50:.0f}",
            "-" if s.p95 is None else f"{s.p95:.0f}",
        )
    console.print(table)
    store.close()
    raise typer.Exit(1 if failed else 0)


def _fmt(v, spec="{:.0f}"):
    return "-" if v is None else spec.format(v)


def _print_map(g: dict, full: bool = True):
    counts = {}
    for e in g["edges"]:
        counts[e["origin"]] = counts.get(e["origin"], 0) + 1
    console.print(
        f"nodes: {len(g['nodes'])}  edges: {len(g['edges'])}  "
        + "  ".join(f"{k}: {v}" for k, v in sorted(counts.items()))
        + f"  dynamic source: {g['dynamic_source'] or 'none'}",
        markup=False,
    )
    for n in g["notes"]:
        console.print(f"note: {n}", markup=False)
    if full:
        t = Table("source", "target", "origin", "via", "calls")
        t.columns[3].overflow = "fold"
        for e in g["edges"]:
            t.add_row(e["source"], e["target"], e["origin"], ",".join(e["via"]), str(e["calls"]))
        console.print(t)
    jt = Table("journey", "from", "services")
    for j, v in g["journeys"].items():
        jt.add_row(j, v["source"], ", ".join(v["services"]))
    console.print(jt)
    console.print(
        "hidden dependency candidates (traces only): " + (", ".join(g["hidden"]["traces_only"]) or "none"),
        markup=False,
    )
    console.print(
        "static only (never seen in traces): " + (", ".join(g["hidden"]["static_only"]) or "none"), markup=False
    )
    if full:
        console.print("\nMermaid:", markup=False)
        print(mapper.mermaid(g))


@app.command("map")
def map_cmd(lookback: int = typer.Option(None, "--lookback", help="Jaeger lookback minutes")):
    """Build the service dependency map (static + traces) and write /data/map.json."""
    cfg = load_config()
    if lookback:
        cfg.map.lookback_min = lookback
    overrides = os.environ.get("FAULTLINE_OVERRIDES", "/app/map.overrides.yaml")
    g = mapper.build_map(cfg, overrides, _data_dir() / "map.json")
    _print_map(g)
    console.print(f"wrote {_data_dir() / 'map.json'}", markup=False)


def _print_baseline(b: dict):
    console.print(
        f"baseline #{b['id']} at {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(b['ts']))}  "
        f"params: {b['params']}",
        markup=False,
    )
    t = Table("journey", "n", "success %", "p50 ms", "p95 ms", "mean ms", "stddev ms", "err max %", "p95 max ms")
    for j, s in b["journeys"].items():
        th = b["thresholds"][j]
        t.add_row(
            j, str(s["count"]), _fmt(None if s["success_rate"] is None else s["success_rate"] * 100, "{:.1f}"),
            _fmt(s["p50_ms"]), _fmt(s["p95_ms"]), _fmt(s["mean_ms"]), _fmt(s["stddev_ms"]),
            f"{th['error_rate_max'] * 100:.1f}", f"{th['p95_max_ms']:.0f}",
        )
    console.print(t)
    svc = b.get("services")
    if svc:
        st = Table("service", "calls/s", "error %", "p95 ms (prometheus)")
        for name, v in sorted(svc.items(), key=lambda kv: -(kv[1].get("p95_ms") or 0))[:12]:
            st.add_row(name, _fmt(v.get("rate"), "{:.2f}"), _fmt(100 * v.get("error_ratio", 0), "{:.2f}"), _fmt(v.get("p95_ms")))
        console.print(st)
    elif b.get("services_note"):
        console.print(b["services_note"], markup=False)


@app.command()
def baseline(
    seconds: int = typer.Option(None, "--seconds", "-s", min=1),
    warmup: int = typer.Option(None, "--warmup", "-w", min=0),
    show: bool = typer.Option(False, "--show", help="Print the latest stored baseline"),
):
    """Measure steady state, derive thresholds, and store them."""
    store = Store()
    if show:
        b = bl.latest(store)
        if not b:
            console.print("no baseline stored")
            raise typer.Exit(1)
        _print_baseline(b)
        return
    cfg = load_config()
    _baseline_run(store, cfg, seconds or cfg.baseline.seconds, cfg.baseline.warmup if warmup is None else warmup)


def _baseline_run(store, cfg, seconds, warmup) -> dict:
    t0 = time.time()
    console.print(f"probing {warmup}s warm-up + {seconds}s measurement", markup=False)
    try:
        asyncio.run(_probe_for(store, warmup + seconds, cfg.watchdog.probe_timeout_s))
    except KeyboardInterrupt:
        console.print("interrupted")
        raise typer.Exit(1)
    rows = {j: store.query(journey=j, since=t0 + warmup) for j in JOURNEYS}
    if not all(rows.values()):
        console.print("no samples for some journeys; is the stack up?")
        raise typer.Exit(1)
    data = bl.compute(rows, cfg)
    try:
        data["services"] = observer.prom_service_stats(max(60, seconds))
        if not data["services"]:
            data["services_note"] = "prometheus returned no span metrics"
    except Exception as e:
        data["services"], data["services_note"] = {}, f"prometheus unavailable: {type(e).__name__}"
    bid = bl.save(store, data, {"seconds": seconds, "warmup": warmup})
    b = bl.latest(store)
    assert b["id"] == bid
    _print_baseline(b)
    return b


def _safety_line(e: dict) -> str:
    line = f"safety agent: {safety_mod.describe(e['safety_decisions'])}"
    return line + (f"; aborted by {e['abort_source']}" if e["state"] == "ABORTED" and e["abort_source"] else "")


def _review_line(rec: dict | None) -> str:
    return f"Safety review: {safety_mod.describe_review(rec)}"


def _print_parts(e: dict, store):
    console.print(analyzer.fault_label(e["fault"], e["params"]), markup=False, highlight=False)
    entries = {(x["kind"], x["target"]): x for x in Ledger(store).all() if x["experiment_id"] == e["id"]} if store else {}
    for i, p in enumerate(e["params"]["parts"], 1):
        x = entries.get((p["fault"], p["target"]))
        console.print(
            f"  part {i}: {analyzer.part_label(p)}" + (f" (ledger #{x['id']} {x['state']})" if x else ""), markup=False, highlight=False
        )


def _print_experiment(e: dict, store=None):
    t = Table("id", "fault", "target", "state", "recovery s", "abort reason")
    rec = e["recovery_seconds"]
    t.add_row(
        e["id"], e["fault"], e["target"], e["state"],
        "not measured" if rec is None and e.get("recovery_measured") == 0 else "never" if rec is None and e["reverted_at"] else _fmt(rec, "{:.1f}"),
        e["abort_reason"] or "-",
        style="red" if e["state"] in ("ABORTED", "VETOED") else "green",
    )
    console.print(t)
    if e["fault"] == faults.COMBO:
        _print_parts(e, store)
    if e["preflight_review"]:
        console.print(_review_line(e["preflight_review"]), markup=False, highlight=False)
    console.print(_safety_line(e), markup=False, highlight=False)
    if e["impact"]:
        it = Table("journey", "n", "err % (fault)", "err % (base)", "p95 ms (fault)", "p95 ms (base)")
        for j, v in e["impact"].items():
            it.add_row(
                j, str(v["count"]), _fmt(None if v["error_rate"] is None else v["error_rate"] * 100, "{:.1f}"),
                _fmt(None if v["base_error_rate"] is None else v["base_error_rate"] * 100, "{:.1f}"),
                _fmt(v["p95_ms"]), _fmt(v["base_p95_ms"]),
            )
        console.print(it)


def _revert_own(store, driver, cfg):
    inj = Injector(Ledger(store), driver, cfg)
    for exp_id in sorted(exp_mod.OWN):
        inj.revert_experiment(exp_id)


def _run_experiment(store, cfg, fault, target, duration, params, context=None, graph=None, review=False) -> dict:
    driver = faults.DockerDriver()
    try:
        return asyncio.run(
            exp_mod.run_experiment(
                store, driver, cfg, fault, target, duration, params, log=lambda m: console.print(m, markup=False, highlight=False),
                context=context, auto_review=review, graph=graph,
            )
        )
    except faults.ValidationError as e:
        console.print(f"rejected: {e}", markup=False)
        raise typer.Exit(2)
    except KeyboardInterrupt:
        console.print("interrupted; reverting", markup=False)
        _revert_own(store, driver, cfg)
        raise typer.Exit(1)


@app.command()
def experiment(
    fault: str = typer.Option(None, "--fault", help="crash | delay | spike (not with --combo)"),
    target: str = typer.Option(None, "--target", help="service; spike always targets the whole shop (traffic)"),
    duration: int = typer.Option(..., "--duration", min=1),
    delay_ms: float = typer.Option(200, "--delay-ms"),
    jitter_ms: float = typer.Option(0, "--jitter-ms"),
    loss_pct: float = typer.Option(0, "--loss-pct"),
    users: int = typer.Option(50, "--users", help="spike: Locust users to ramp to"),
    spawn_rate: float = typer.Option(25, "--spawn-rate", help="spike: users started per second"),
    combo: list[str] = typer.Option(
        None, "--combo",
        help="combined failure: repeat once per part as fault:target[:key=value,...], e.g. --combo spike:traffic:users=100 --combo delay:quote:delay_ms=50",
    ),
):
    """Run one guarded fault injection (or a declared combination) with watchdog, revert, and recovery measurement."""
    cfg = load_config()
    if combo:
        if fault or target:
            console.print("rejected: --combo replaces --fault and --target", markup=False)
            raise typer.Exit(2)
        try:
            fault, params = faults.COMBO, {"parts": [combo_mod.parse(x) for x in combo]}
            target = combo_mod.target_key(faults.validate(cfg, fault, "", duration, params)["parts"])
        except faults.ValidationError as e:
            console.print(f"rejected: {e}", markup=False)
            raise typer.Exit(2)
    elif not fault:
        console.print("rejected: --fault or --combo is required", markup=False)
        raise typer.Exit(2)
    elif fault == "spike":
        target = target or faults.TRAFFIC
        params = {"users": users, "spawn_rate": spawn_rate}
    else:
        if not target:
            console.print("rejected: --target is required", markup=False)
            raise typer.Exit(2)
        params = {"delay_ms": delay_ms, "jitter_ms": jitter_ms, "loss_pct": loss_pct}
    store = Store()
    context = gmap = None
    try:
        gmap = planner_mod.load_map(cfg, os.environ.get("FAULTLINE_OVERRIDES", "/app/map.overrides.yaml"), _data_dir() / "map.json")
        context = safety_mod.context_from_map(gmap, target)
    except Exception:
        pass
    e = _run_experiment(store, cfg, fault, target, duration, params, context, gmap, review=True)
    _print_experiment(e, store)
    raise typer.Exit(0 if e["state"] == "COMPLETED" else 1)


def _print_ledger(store, limit=20):
    entries = Ledger(store).all()[-limit:]
    t = Table("id", "experiment", "kind", "target", "state", "applied", "reverted", "error")
    ts = lambda v: "-" if not v else time.strftime("%H:%M:%S", time.localtime(v))
    for e in entries:
        t.add_row(str(e["id"]), e["experiment_id"] or "-", e["kind"], e["target"], e["state"], ts(e["applied_at"]), ts(e["reverted_at"]), e["error"] or "-")
    console.print(t)
    return entries


@app.command()
def ledger(limit: int = typer.Option(20, "--limit", "-n")):
    """Show the fault ledger."""
    _print_ledger(Store(), limit)


@app.command()
def stop():
    """Kill switch: revert every open ledger entry and remove faultline sidecars."""
    cfg = load_config()
    store = Store()
    sentinel = exp_mod.stop_path()
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text(str(time.time()))
    try:
        inj = Injector(Ledger(store), faults.DockerDriver(), cfg)
        results = inj.revert_all()
        time.sleep(2.5)
    finally:
        sentinel.unlink(missing_ok=True)
    left = Ledger(store).open_entries()
    console.print(f"reverted: {results or 'nothing open'}", markup=False)
    if left:
        console.print(f"still open: {[e['id'] for e in left]}", markup=False)
        raise typer.Exit(1)


@app.command()
def reaper():
    """Dead-man's switch: revert expired faults and everything when the controller heartbeat goes stale."""
    cfg = load_config()
    store = Store()
    flag = {"stop": False}
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: flag.update(stop=True))
    reaper_mod.run(store, faults.DockerDriver(), cfg, log=lambda m: print(m, flush=True), stop=lambda: flag["stop"])


@app.command()
def demo(day1: bool = typer.Option(False, "--day1", help="Run the Day 1 end-to-end demo")):
    """Run the scripted demo."""
    if not day1:
        console.print("pass --day1")
        raise typer.Exit(2)
    cfg = load_config()
    store = Store()
    head = lambda s: console.print(f"\n=== {s} ===", markup=False)

    head("1. STATUS")
    _status_table()

    head("2. MAP")
    g = mapper.build_map(cfg, os.environ.get("FAULTLINE_OVERRIDES", "/app/map.overrides.yaml"), _data_dir() / "map.json")
    _print_map(g, full=False)

    head("3. BASELINE (30s + 5s warm-up)")
    _baseline_run(store, cfg, 30, 5)

    target, fault, val = DEMO_SAFE
    head(f"4. SAFE EXPERIMENT: {fault} {val}ms on {target}, expected to COMPLETE")
    params = {"delay_ms": val}
    e1 = _run_experiment(store, cfg, fault, target, 20, params)
    _print_experiment(e1)

    target, fault, dur = DEMO_BREACH
    head(f"5. BREACHING EXPERIMENT: {fault} {target}, expected to be ABORTED by the watchdog")
    e2 = _run_experiment(store, cfg, fault, target, dur, {})
    _print_experiment(e2)

    head("6. LEDGER")
    entries = _print_ledger(store, 10)
    ok = bool(entries) and all(e["state"] == REVERTED for e in entries)
    console.print("all ledger entries REVERTED" if ok else "LEDGER HAS OPEN ENTRIES", markup=False)
    result = e1["state"] == "COMPLETED" and e2["state"] == "ABORTED" and ok
    console.print(f"demo {'PASSED' if result else 'FAILED'}", markup=False)
    raise typer.Exit(0 if result else 1)


@app.command()
def plan(top: int = typer.Option(None, "--top", min=1, help="Candidates ranked and sent to the LLM")):
    """Rank fault experiments (deterministic score + Groq) and validate the pick."""
    cfg = load_config()
    store = Store()
    overrides = os.environ.get("FAULTLINE_OVERRIDES", "/app/map.overrides.yaml")
    gmap = planner_mod.load_map(cfg, overrides, _data_dir() / "map.json")
    state = plan_graph.run_plan(cfg, gmap, store, top)
    res = state["plan"]
    t = Table("rank", "candidate", "target", "fault", "params", "det_score", "llm_rank", "reason")
    t.columns[7].overflow = "fold"
    for i, s in enumerate(res.ranked, 1):
        c = s.candidate
        t.add_row(str(i), c.id, c.target, c.fault, f"{planner_mod.params_text(c)} {c.duration_s}s", f"{s.det_score:.2f}", str(s.llm_rank or "-"), s.reason)
    console.print(t)
    for x in res.exclusions:
        console.print(f"memory: excluded {x['candidate']}: {x['reason']}", markup=False, highlight=False)
    if res.limits:
        console.print(f"memory: {len(res.limits)} learned limits, {len(res.exclusions)} candidates excluded", markup=False)
    pick = state.get("selected")
    for r in state.get("rejected", []):
        console.print(f"validator rejected {r['candidate']}: {r['reason']}", markup=False)
    if pick:
        hyp, blast = planner_mod.hypothesis_for(res, gmap, pick)
        c = pick.candidate
        console.print(f"selected: {c.fault} {c.target} {'' if c.fault == faults.COMBO else c.params or ''} {c.duration_s}s ({c.id})", markup=False)
        console.print(f"hypothesis: {hyp}", markup=False)
        console.print(f"predicted blast radius: {', '.join(blast) or 'none'}", markup=False)
        console.print(f"validator: ok ({state['validation'][1]})", markup=False)
    else:
        console.print(f"no plan: validator: {state['validation'][1] if state.get('validation') else 'no candidates'}", markup=False)
    if res.fallback:
        console.print(f"planner: FALLBACK ({res.fallback_reason})", markup=False)
    else:
        console.print(f"planner: groq {res.model} ({res.latency_ms:.0f} ms{', cached' if res.cached else ''})", markup=False)
    raise typer.Exit(0 if pick else 1)


def _round_rec(r: dict) -> str:
    if r["recovery_s"] is not None:
        return _fmt(r["recovery_s"], "{:.1f}")
    return "not measured" if runs_mod.is_operator_stop(r["abort_reason"]) else "never"


def _round_source(r: dict) -> str:
    return "FALLBACK" if r["fallback"] else f"LLM #{r['llm_rank'] or '-'}"


def _print_round(r: dict, total: int):
    src = f"FALLBACK ({r.get('fallback_reason')})" if r["fallback"] else "LLM"
    console.print(
        f"round {r['round']}/{total}  tier {r['tier']}  pick {r['candidate_id']} (tier {r.get('target_tier', '-')})", markup=False
    )
    console.print(f"  {src}: {r['reason']}", markup=False, highlight=False)
    console.print(
        f"  outcome {r['outcome']}  recovery {_round_rec(r)}{'' if r['recovery_s'] is None else 's'}"
        + (f"  abort: {r['abort_reason']}" if r["abort_reason"] else ""),
        markup=False, highlight=False,
    )
    console.print(f"  climb: {r['climb_decision']}", markup=False, highlight=False)


def _print_run(run: dict, rows: list[dict]):
    t = Table("round", "tier", "pick", "source", "outcome", "recovery s", "climb decision")
    t.columns[6].overflow = "fold"
    for r in rows:
        t.add_row(
            str(r["round"]), str(r["tier"]), r["candidate_id"], _round_source(r), r["outcome"] or "-",
            _round_rec(r), r["climb_decision"] or "-",
            style="red" if r["outcome"] == "ABORTED" else None,
        )
    console.print(t)
    console.print(
        f"run {run['run_id']}: rounds {run['rounds_done']}/{run['rounds_planned']}, final tier {run['final_tier'] or '-'}, "
        f"stop: {run['stop_reason'] or 'running'}",
        markup=False,
    )


@app.command("run")
def run_cmd(
    rounds: int = typer.Option(3, "--rounds", "-r", min=1, max=8, help="Experiments to run, climbing the risk ladder"),
    make_report: bool = typer.Option(False, "--report", help="Generate the findings report after the run"),
    fix: bool = typer.Option(False, "--fix", help="After the report, apply and verify the top auto-applicable fix (needs --report and --yes)"),
    yes: bool = typer.Option(False, "--yes", help="Confirm applying the fix chosen by --fix"),
):
    """Autonomous run: plan, validate, execute, record, repeat; start at tier 1 and climb as experiments survive."""
    cfg = load_config()
    if fix and not (make_report and yes):
        console.print("rejected: --fix needs --report and --yes (it changes a running container)", markup=False)
        raise typer.Exit(2)
    store = Store()
    overrides = os.environ.get("FAULTLINE_OVERRIDES", "/app/map.overrides.yaml")
    gmap = planner_mod.load_map(cfg, overrides, _data_dir() / "map.json")
    if bl.latest(store) is None:
        console.print("no baseline; run `faultline baseline` first")
        raise typer.Exit(2)
    driver = faults.DockerDriver()
    exp_mod.stop_path().unlink(missing_ok=True)
    with runs_mod.RunControl() as control:
        control.install_signals()

        def runner(c, context=None, review=None):
            try:
                return asyncio.run(exp_mod.run_experiment(
                    store, driver, cfg, c.fault, c.target, c.duration_s, dict(c.params),
                    log=lambda m: console.print(m, markup=False, highlight=False), context=context, review=review,
                ))
            except KeyboardInterrupt:
                console.print("interrupted; reverting", markup=False)
                _revert_own(store, driver, cfg)
                control.request("operator interrupt (Ctrl+C)")
                return None
            finally:
                control.install_signals()

        try:
            run = plan_graph.run_loop(
                cfg, gmap, store, rounds, runner, lambda: runs_mod.wait_for_baseline(store, cfg, control), control,
                log=lambda m: console.print(m, markup=False, highlight=False), on_round=lambda r: _print_round(r, rounds),
            )
        except KeyboardInterrupt:
            _revert_own(store, driver, cfg)
            run = runs_mod.get(store)
    console.print()
    _print_run(run, runs_mod.rounds(store, run["run_id"]))
    open_left = Ledger(store).open_entries()
    if open_left:
        console.print(f"LEDGER HAS OPEN ENTRIES: {[e['id'] for e in open_left]}", markup=False)
    fix_ok = True
    if make_report:
        state = _make_report(cfg, store, gmap, run["run_id"])
        if fix and state is not None and not open_left:
            fix_ok = _fix_flow(cfg, store, gmap, _proposals(cfg, store, state["data"]), run["run_id"], None, True, None)
            if fix_ok:
                _make_report(cfg, store, gmap, run["run_id"])
    raise typer.Exit(0 if run["rounds_done"] == run["rounds_planned"] and not open_left and fix_ok else 1)


def _reports_dir() -> Path:
    return Path(os.environ.get("FAULTLINE_REPORTS", "/app/reports"))


def _make_report(cfg, store, gmap, run_id) -> dict | None:
    state = plan_graph.run_report(cfg, gmap, store, _reports_dir(), run_id)
    if state is None:
        console.print("no such run" if run_id else "no runs stored")
        return None
    d = state["data"]
    console.print(f"report for {d['run']['run_id']}: overall resilience score {_fmt(d['overall_score'], '{:.1f}')} ({d['rating']})", markup=False)
    for k, p in state["paths"].items():
        console.print(f"  {k}: {p}", markup=False)
    for w in d["weaknesses"][:3]:
        console.print(f"  weakness {w['rank']} [{w['severity']}] {w['title']}", markup=False, highlight=False)
    for u in d["ai_usage"]:
        console.print(f"  agent {u['agent']}: {u['path']} ({u['prompt_tokens']}/{u['completion_tokens']} tokens)", markup=False, highlight=False)
    return state


@app.command()
def report(run_id: str = typer.Option(None, "--run", help="Run id (default: latest)")):
    """Analyze a stored run and write report.md, report.html and report.json under reports/<run_id>/."""
    cfg = load_config()
    store = Store()
    overrides = os.environ.get("FAULTLINE_OVERRIDES", "/app/map.overrides.yaml")
    gmap = planner_mod.load_map(cfg, overrides, _data_dir() / "map.json")
    raise typer.Exit(0 if _make_report(cfg, store, gmap, run_id) else 1)


def _proposals(cfg, store, data: dict) -> list[dict]:
    """Weaknesses of a report.json with their stored fix, in rank order."""
    out = []
    for w in data["weaknesses"]:
        if w.get("fix"):
            out.append({"rank": w["rank"], "title": w["title"], "fix": fixer_mod.Fix(**w["fix"]), "experiment_ids": w["related_experiment_ids"]})
    return out


def _run_proposals(cfg, store, gmap, run_id) -> tuple[str, list[dict]]:
    run = runs_mod.get(store, run_id)
    if not run:
        console.print("no such run" if run_id else "no runs stored")
        raise typer.Exit(1)
    path = _reports_dir() / run["run_id"] / "report.json"
    if path.is_file():
        data = json.loads(path.read_text())
    else:
        console.print("no stored report for this run; generating it (analyst and fixer)", markup=False)
        state = _make_report(cfg, store, gmap, run["run_id"])
        if state is None:
            raise typer.Exit(1)
        data = state["data"]
    return run["run_id"], _proposals(cfg, store, data)


def _experiment_proposals(cfg, store, gmap, exp_id, use_fixer=True) -> list[dict]:
    try:
        exp = exp_mod.get(store, exp_id)
    except TypeError:
        console.print("no such experiment", markup=False)
        raise typer.Exit(1)
    base = bl.latest(store)
    analysis = {"experiments": [analyzer.analyze_experiment(cfg, exp, None, gmap, base["thresholds"] if base else None)]}
    ws = analyst_mod.fallback_weaknesses(cfg, analysis)
    a = analysis["experiments"][0]
    if not ws and not use_fixer:
        ws = [analyst_mod.Weakness(
            rank=1, title=f"{a['label']} on {a['target']} (operator-selected experiment)", severity=analyzer.severity_of(a["penalty"]),
            services=[], explanation="No weakness at the reporting threshold; the experiment was chosen with --override.",
            related_experiment_ids=[exp_id], source="operator",
        )]
    if not ws:
        console.print(f"{exp_id} shows no weakness at or above weak_min_penalty {cfg.score.weak_min_penalty:g}; nothing to fix", markup=False)
        raise typer.Exit(1)
    if not use_fixer:
        stub = [fixer_mod.Fix(weakness_rank=w.rank, fix_title="Operator override", fix_type="config", steps="Supplied with --override.", source="operator") for w in ws]
        return [{"rank": w.rank, "title": w.title, "fix": f, "experiment_ids": w.related_experiment_ids} for w, f in zip(ws, stub)]
    res = fixer_mod.fix(cfg, store, analysis, gmap, ws)
    console.print(f"fixer: {'FALLBACK (' + str(res.fallback_reason) + ')' if res.fallback else 'groq ' + res.model}", markup=False)
    return [{"rank": w.rank, "title": w.title, "fix": f, "experiment_ids": w.related_experiment_ids} for w, f in zip(ws, res.fixes)]


def _hc_line(c) -> str:
    c.reload()
    hc = c.attrs["HostConfig"]
    rp = hc.get("RestartPolicy") or {}
    return (f"{c.name}: restart {rp.get('Name') or 'no'}, memory {(hc.get('Memory') or 0) / fixes_mod.MB:g} MB, "
            f"memswap {(hc.get('MemorySwap') or 0) / fixes_mod.MB:g} MB, cpu quota {hc.get('CpuQuota') or 0}")


def _print_fix_result(f: dict):
    d = f["details"] or {}
    t = Table("experiments", "journey", "p95 ms before", "p95 ms after", "err % before", "err % after")
    pct = lambda e: "-" if e is None else f"{100 * e:.1f}"
    for pr in d.get("pairs", []):
        for j, v in pr["journeys"].items():
            t.add_row(f"{pr['before']} -> {pr['after']}", j, _fmt(v["p95_before"]), _fmt(v["p95_after"]), pct(v["err_before"]), pct(v["err_after"]))
        t.add_row(f"{pr['before']} -> {pr['after']}", "PENALTY", _fmt(pr["before_penalty"], "{:.1f}"), _fmt(pr["after_penalty"], "{:.1f}"), "-", "-", style="bold")
    console.print(t)
    if d.get("reason"):
        console.print(f"not verified: {d['reason']}", markup=False, highlight=False)
    console.print(
        f"fix #{f['id']} on {f['target']}: mean penalty {_fmt(f['before_penalty'], '{:.1f}')} -> {_fmt(f['after_penalty'], '{:.1f}')}, "
        f"verdict {f['verdict']}, state {f['state']}", markup=False, highlight=False,
    )


def _fix_flow(cfg, store, gmap, proposals, run_id, weakness, yes, override, client=None) -> bool:
    if override is not None:
        pick = next((p for p in proposals if weakness in (None, p["rank"])), None)
        if pick is None:
            console.print("no weakness to attach the override to", markup=False)
            return False
        text, why = fixer_mod.check_override(override, set(gmap["nodes"]))
        if text is None:
            console.print(f"override rejected: {why}", markup=False)
            return False
        pick["fix"] = fixer_mod.Fix(weakness_rank=pick["rank"], fix_title="Operator override", fix_type="config", steps="Supplied with --override.", compose_override=text, source="operator")
    t = Table("rank", "weakness", "fix", "applicable")
    t.columns[3].overflow = "fold"
    chosen = None
    for p in proposals:
        change, why = fixes_mod.applicable(cfg, p["fix"])
        t.add_row(str(p["rank"]), p["title"][:60], p["fix"].fix_title[:50], fixes_mod.describe_change(change) if change else why)
        if change and chosen is None and weakness in (None, p["rank"]):
            chosen = (p, change)
    console.print(t)
    if chosen is None:
        console.print("no auto-applicable fix; the proposals stay propose-only", markup=False)
        return False
    p, change = chosen
    import docker

    applier = fixes_mod.Applier(store, cfg, client or docker.from_env())
    try:
        _, lines = applier.preview(change)
    except fixes_mod.Refused as e:
        console.print(f"not applicable now: {e}", markup=False)
        return False
    console.print(f"weakness {p['rank']}: {p['title']}", markup=False, highlight=False)
    console.print(f"change on {change['service']}: " + "; ".join(lines), markup=False, highlight=False)
    if change["skipped"]:
        console.print(f"propose-only part, not applied: {', '.join(change['skipped'])}", markup=False)
    if not yes and not (sys.stdin.isatty() and typer.confirm("apply this change to the running container?")):
        console.print("not applied: pass --yes to confirm", markup=False)
        return False
    befores = []
    for i in p["experiment_ids"]:
        e = exp_mod.get(store, i)
        if e["injected_at"] is not None and e["state"] != "VETOED":
            befores.append((fixes_mod.penalty_of(cfg, store, e, gmap), e))
    befores = [e for _, e in sorted(befores, key=lambda x: -x[0])[: cfg.fix.rerun_max]]
    if not befores:
        console.print("the weakness has no injected experiment to re-run", markup=False)
        return False

    def rerun(e):
        fault, target, duration, params = fixes_mod.rerun_args(e)
        ctx = None
        try:
            ctx = safety_mod.context_from_map(gmap, target)
        except Exception:
            pass
        return _run_experiment(store, cfg, fault, target, duration, params, ctx, gmap, review=True)

    try:
        f = fixes_mod.run_fix(store, cfg, applier, change, befores, rerun, gmap, run_id, p["rank"], log=lambda m: console.print(m, markup=False, highlight=False))
    except fixes_mod.Refused as e:
        console.print(f"fix refused: {e}", markup=False)
        return False
    _print_fix_result(f)
    return f["verdict"] != fixes_mod.INCONCLUSIVE and f["state"] != fixes_mod.REVERT_FAILED


@app.command("fix")
def fix_cmd(
    run_id: str = typer.Option(None, "--run", help="Run whose report weaknesses to fix (generated if missing)"),
    experiment: str = typer.Option(None, "--experiment", help="Fix the weakness shown by one experiment (calls the Fixer)"),
    weakness: int = typer.Option(None, "--weakness", min=1, help="Weakness rank (default: top auto-applicable)"),
    yes: bool = typer.Option(False, "--yes", help="Apply without asking"),
    override: Path = typer.Option(None, "--override", help="Compose override YAML to apply instead of the Fixer's (same checks)"),
    revert: str = typer.Option(None, "--revert", help="Revert one fix by id, or `latest` for the newest unreverted one"),
    revert_all: bool = typer.Option(False, "--revert-all", help="Restore the original values of every unreverted fix"),
    list_all: bool = typer.Option(False, "--list", help="List recorded fixes"),
):
    """Apply a Fixer proposal to a running container (restart policy, raised memory or CPU limit), re-run the experiment and keep it only if it helps."""
    cfg = load_config()
    store = Store()
    if list_all:
        t = Table("id", "target", "state", "weakness", "before", "after", "verdict", "error")
        for f in fixes_mod.Fixes(store).all():
            t.add_row(str(f["id"]), f["target"], f["state"], str(f["weakness_rank"] or "-"), _fmt(f["before_penalty"], "{:.1f}"), _fmt(f["after_penalty"], "{:.1f}"), f["verdict"] or "-", f["error"] or "-")
        console.print(t)
        return
    if revert is not None or revert_all:
        import docker

        applier = fixes_mod.Applier(store, cfg, docker.from_env())
        open_ = applier.fixes.open()
        if revert is not None and not revert_all:
            ids = [open_[-1]["id"]] if revert == "latest" and open_ else [int(revert)] if revert.isdigit() else []
            open_ = [f for f in applier.fixes.all() if f["id"] in ids]
        if not open_:
            console.print("no unreverted fix", markup=False)
            return
        bad = False
        for f in open_:
            try:
                console.print("before: " + _hc_line(applier.container(f["target"])), markup=False, highlight=False)
            except fixes_mod.Refused as e:
                console.print(f"before: {e}", markup=False)
            state = applier.revert(f["id"])
            bad = bad or state != fixes_mod.REVERTED
            try:
                console.print("after:  " + _hc_line(applier.container(f["target"])), markup=False, highlight=False)
            except fixes_mod.Refused as e:
                console.print(f"after: {e}", markup=False)
            console.print(f"fix #{f['id']} on {f['target']}: {state}", markup=False)
        raise typer.Exit(1 if bad else 0)
    if bool(run_id) == bool(experiment):
        console.print("rejected: give exactly one of --run RUN_ID and --experiment EXPERIMENT_ID", markup=False)
        raise typer.Exit(2)
    overrides_path = os.environ.get("FAULTLINE_OVERRIDES", "/app/map.overrides.yaml")
    gmap = planner_mod.load_map(cfg, overrides_path, _data_dir() / "map.json")
    if bl.latest(store) is None:
        console.print("no baseline; run `faultline baseline` first")
        raise typer.Exit(2)
    text = override.read_text() if override else None
    if experiment:
        rid, proposals = None, _experiment_proposals(cfg, store, gmap, experiment, use_fixer=text is None)
    else:
        rid, proposals = _run_proposals(cfg, store, gmap, run_id)
    ok = _fix_flow(cfg, store, gmap, proposals, rid, weakness, yes, text)
    raise typer.Exit(0 if ok else 1)


@runs_app.command("show")
def runs_show(run_id: str = typer.Argument(None, help="Run id (default: latest)")):
    """Print a stored run (latest by default)."""
    store = Store()
    run = runs_mod.get(store, run_id)
    if not run:
        console.print("no such run" if run_id else "no runs stored")
        raise typer.Exit(1)
    rows = runs_mod.rounds(store, run["run_id"])
    _print_run(run, rows)
    for x in (run["memory"] or {}).get("exclusions", []):
        console.print(f"memory: excluded {x['candidate']}: {x['reason']}", markup=False, highlight=False)
    for r in rows:
        console.print(f"round {r['round']} {r['candidate_id']}: {r['hypothesis']}", markup=False, highlight=False)
        console.print(
            f"  predicted blast radius: {', '.join(r['predicted_blast_radius']) or 'none'}; experiment {r['experiment_id']}"
            + (f"; abort: {r['abort_reason']}" if r["abort_reason"] else ""),
            markup=False, highlight=False,
        )
        pf = r["preflight"]
        if pf:
            for v in pf.get("vetoed", []):
                console.print(f"  safety review vetoed {v['candidate']}: {v['reason']}", markup=False, highlight=False)
            if pf.get("decision"):
                console.print("  " + _review_line(pf), markup=False, highlight=False)
        if r["experiment_id"]:
            e = exp_mod.get(store, r["experiment_id"])
            console.print("  " + _safety_line(e), markup=False, highlight=False)
            for c in e["safety_decisions"]:
                what = f"{c['decision']} ({c['concern']}) {c['reason']}" if c["decision"] else f"ignored ({c['error']})"
                console.print(f"    t={c['t']:g}s {what} [{c['latency_ms']} ms, {c['tokens']} tok]", markup=False, highlight=False)


@agents_app.command("trace")
def agents_trace(limit: int = typer.Option(20, "--limit", "-n")):
    """Show recent agent trace rows."""
    t = Table("id", "time", "agent", "model", "ms", "tok", "fb", "input", "output", "error")
    for col in (7, 8, 9):
        t.columns[col].overflow = "fold"
    for r in Store().traces(limit):
        t.add_row(
            str(r["id"]), time.strftime("%H:%M:%S", time.localtime(r["ts"])), r["agent"], r["model"] or "-",
            _fmt(r["latency_ms"]), f"{r['prompt_tokens']}/{r['completion_tokens']}" if r["prompt_tokens"] is not None else "-",
            "yes" if r["fallback"] else "no", (r["input_summary"] or "-")[:60],
            (r["output_json"] or "-")[:70], r["error"] or "-",
        )
    console.print(t)


@agents_app.command("usage")
def agents_usage():
    """Today's per-model LLM requests and tokens against the daily budget, plus cache hits."""
    cfg = load_config()
    store = Store()
    used = {r["model"]: r for r in store.usage_all_today()}
    models = sorted({cfg.llm.planner_model, cfg.llm.analyst_model, cfg.llm.fixer_model, cfg.llm.safety_model} | set(used))
    t = Table("model", "requests", "prompt", "completion", "total", "budget", "used %")
    for m in models:
        u = store.usage_today(m)
        t.add_row(
            m, str(u["requests"]), str(u["prompt_tokens"]), str(u["completion_tokens"]), str(u["total_tokens"]),
            str(cfg.llm.daily_token_budget), f"{100 * u['total_tokens'] / cfg.llm.daily_token_budget:.1f}",
        )
    console.print(t)
    console.print(f"cache hits today: {store.cache_hits_today()}", markup=False)
