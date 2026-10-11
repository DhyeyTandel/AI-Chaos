import asyncio
import json
import secrets
import signal
import time
from pathlib import Path

from . import baseline as bl
from . import analyzer, combo, faults, watchdog
from .injector import Injector
from .ledger import REVERTED, Ledger
from .agents import injection, recovery as recovery_agent, safety
from .prober import run_probe
from .store import Store, default_db_path

STOP_NAME = "STOP"
OWN: set[str] = set()


def stop_path() -> Path:
    return Path(default_db_path()).parent / STOP_NAME


def new_id() -> str:
    return "exp-" + time.strftime("%H%M%S") + "-" + secrets.token_hex(2)


def _update(store, exp_id, **cols):
    keys = ", ".join(f"{k} = ?" for k in cols)
    with store.conn:
        store.conn.execute(f"UPDATE experiments SET {keys} WHERE id = ?", (*cols.values(), exp_id))


def _with_injector(db_path, driver, cfg, fn):
    store = Store(db_path)
    try:
        return fn(Injector(Ledger(store), driver, cfg), store)
    finally:
        store.close()


def get(store, exp_id) -> dict:
    r = dict(store.conn.execute("SELECT * FROM experiments WHERE id = ?", (exp_id,)).fetchone())
    r["params"] = json.loads(r["params"])
    r["impact"] = json.loads(r["impact"]) if r["impact"] else None
    r["safety_decisions"] = json.loads(r["safety_decisions"]) if r["safety_decisions"] else []
    r["preflight_review"] = json.loads(r["preflight_review"]) if r["preflight_review"] else None
    r["injection"] = json.loads(r["injection"]) if r.get("injection") else None
    r["recovery_agent"] = json.loads(r["recovery_agent"]) if r.get("recovery_agent") else None
    return r


def _record_veto(store, fault, target, params, review):
    exp_id = new_id()
    now = time.time()
    with store.conn:
        store.conn.execute(
            "INSERT INTO experiments (id, fault, target, params, state, created_at, finished_at, abort_reason, preflight_review)"
            " VALUES (?, ?, ?, ?, 'VETOED', ?, ?, ?, ?)",
            (exp_id, fault, target, json.dumps(params), now, now, f"safety review veto: {review['reason']}", json.dumps(review)),
        )
    return exp_id


async def run_experiment(
    store, driver, cfg, fault, target, duration, params=None, log=print, stop: asyncio.Event | None = None, context=None, chat=None,
    review=None, auto_review=False, graph=None, tier=None,
):
    params = faults.validate(cfg, fault, target, duration, params or {})
    is_combo = fault == faults.COMBO
    if is_combo:
        target = combo.target_key(params["parts"])
    base = bl.latest(store)
    if base is None:
        raise faults.ValidationError("no baseline; run `faultline baseline` first")
    if auto_review and review is None:
        review = safety.preflight_review(cfg, store, base, fault, target, duration, params, tier, context, graph, chat)
        if review:
            log(f"Safety review: {safety.describe_review(review)}")
        if review and review["decision"] == "veto":
            return get(store, _record_veto(store, fault, target, params, review))
        if review and review["decision"] == "reduce":
            duration = review["final"]["duration_s"]
            params = faults.validate(cfg, fault, target, duration, review["final"]["params"])
    thresholds = base["thresholds"]
    live = combo.tighten(cfg, thresholds) if is_combo else thresholds
    stop = stop or asyncio.Event()
    db_path = store.path
    ledger = Ledger(store)
    exp_id = new_id()
    OWN.add(exp_id)
    with store.conn:
        store.conn.execute(
            "INSERT INTO experiments (id, fault, target, params, state, created_at, preflight_review)"
            " VALUES (?, ?, ?, ?, 'PREFLIGHT', ?, ?)",
            (exp_id, fault, target, json.dumps(params), time.time(), json.dumps(review) if review else None),
        )
    sp = stop_path()
    sp.unlink(missing_ok=True)

    loop = asyncio.get_running_loop()
    installed = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
            installed.append(sig)
        except (NotImplementedError, RuntimeError):
            pass

    def rows_since(t):
        return store.query(since=t)

    async def beat():
        while True:
            ledger.beat(exp_id)
            await asyncio.sleep(cfg.reaper.heartbeat_s)

    ra = cfg.recovery_agent
    agent_s = ra.max_actions * (ra.settle_s + ra.remeasure_s + ra.timeout_s + 10) if ra.enabled else 0
    total_cap = cfg.preflight.probe_s + duration + cfg.recovery.cap_s + 60 + agent_s
    probe_stop = asyncio.Event()
    probe_task = asyncio.create_task(
        run_probe(store, total_cap, timeout=cfg.watchdog.probe_timeout_s, stop=probe_stop)
    )
    beat_task = asyncio.create_task(beat())
    state, reason, recovery = "ABORTED", None, None
    inj_rec = ra_rec = None
    t_apply = None
    not_measured = False
    source = None
    monitor = None
    t_inject = t_revert = None
    try:
        log(f"[{exp_id}] PREFLIGHT")
        t_pre = time.time()
        ledger.beat(exp_id)
        entries = len(ledger.open_entries())
        orphans = await asyncio.to_thread(driver.list_owned)
        if entries or orphans:
            reason = f"preflight: {entries} open ledger entries, {len(orphans)} faultline containers"
            source = "preflight"
        else:
            while time.time() - t_pre < cfg.preflight.probe_s and not stop.is_set():
                await asyncio.sleep(0.5)
            now = time.time()
            breaches, judged, short = watchdog.judge(
                rows_since(now - 60), live, now, cfg.preflight.probe_s, cfg.watchdog.min_samples
            )
            if stop.is_set() or sp.exists():
                reason, source = "operator stop during preflight", "operator"
            elif breaches or short:
                reason = "preflight: " + ("; ".join(breaches) or f"insufficient samples for {', '.join(short)}")
                source = "preflight"
        if reason is None:
            _update(store, exp_id, state="INJECTING")
            log(f"[{exp_id}] INJECTING {analyzer.fault_label(fault, params) if is_combo else fault + ' on ' + target} for {duration}s")
            try:
                spec = injection.prepare(cfg, fault, target, params, duration)
                t_apply = time.time()
                inj_rec = await asyncio.to_thread(injection.inject, db_path, driver, cfg, exp_id, spec)
                log(f"[{exp_id}] {injection.describe(inj_rec)}")
            except injection.NotVerified as e:
                inj_rec = e.record
                reason, source = f"injection not verified: {e}", "inject"
                log(f"[{exp_id}] {injection.describe(inj_rec)}")
            except Exception as e:
                reason, source = f"inject failed: {type(e).__name__}: {e}", "inject"
        if reason is None:
            t_inject = time.time()
            _update(store, exp_id, state="OBSERVING", injected_at=t_inject)
            log(f"[{exp_id}] OBSERVING")
            wd = watchdog.Watchdog(live, cfg, t_inject)
            monitor = safety.Monitor(cfg, db_path, {**base, "thresholds": live}, fault, target, params, duration, context, t_inject, chat=chat, log=lambda m: log(f"[{exp_id}] {m}"))
            monitor.start()
            while time.time() < t_apply + duration:
                await asyncio.sleep(cfg.watchdog.interval_s)
                now = time.time()
                if stop.is_set():
                    reason, source = "operator interrupt (Ctrl+C / signal)", "operator"
                elif sp.exists():
                    reason, source = "kill switch (STOP file)", "operator"
                else:
                    d = wd.check(rows_since(now - cfg.watchdog.window_s - 15), now)
                    if d.status != watchdog.OK:
                        reason, source = f"watchdog {d.status}: {d.reason}", "watchdog"
                    elif monitor.abort_reason():
                        reason, source = f"safety agent: {monitor.abort_reason()}", "safety_agent"
                if reason:
                    log(f"[{exp_id}] ABORT: {reason}")
                    break
        if monitor:
            monitor.stop()
        if t_inject is not None:
            _update(store, exp_id, state="REVERTING")
            log(f"[{exp_id}] REVERTING")
        results = await asyncio.to_thread(lambda: _with_injector(db_path, driver, cfg, lambda inj, s: inj.revert_experiment(exp_id)))
        t_revert = time.time() if t_inject is not None else None
        bad = {i: st for i, st in results.items() if st != REVERTED}
        if bad:
            reason = (reason + "; " if reason else "") + f"REVERT_FAILED {bad}"
        if t_inject is not None:
            _update(store, exp_id, reverted_at=t_revert, state="RECOVERING")
            log(f"[{exp_id}] RECOVERING")
            while not bad and time.time() - t_revert < cfg.recovery.cap_s and not stop.is_set():
                await asyncio.sleep(cfg.watchdog.interval_s)
                now = time.time()
                if watchdog.recovered(rows_since(t_revert), thresholds, now, t_revert, cfg):
                    recovery = now - t_revert
                    break
        not_measured = recovery is None and stop.is_set() and not bad
        if t_inject is not None and not bad:
            if recovery is not None or not_measured:
                ra_rec = recovery_agent.settled(store, exp_id, recovery, not not_measured)
            else:
                log(f"[{exp_id}] RECOVERY FAILED: not back in the baseline band after {cfg.recovery.cap_s}s")
                ra_rec = await recovery_agent.remediate(recovery_agent.Ctx(
                    cfg=cfg, store=store, driver=driver, exp=get(store, exp_id), graph=graph, thresholds=thresholds, base=base,
                    t_revert=t_revert, stop=stop, stop_path=sp, t_inject=t_inject, chat=chat, log=lambda m: log(f"[{exp_id}] {m}"),
                ))
                if ra_rec["final_recovery_s"] is not None:
                    recovery = ra_rec["final_recovery_s"]
            log(f"[{exp_id}] {recovery_agent.describe(ra_rec)}")
        state = "COMPLETED" if reason is None else "ABORTED"
    finally:
        probe_stop.set()
        beat_task.cancel()
        for sig in installed:
            loop.remove_signal_handler(sig)
        await asyncio.gather(probe_task, beat_task, return_exceptions=True)
        await asyncio.to_thread(lambda: _with_injector(db_path, driver, cfg, lambda inj, s: inj.revert_experiment(exp_id, wait_s=5)))
        if monitor:
            monitor.stop()
            await asyncio.to_thread(monitor.join, cfg.safety_agent.timeout_s + 2)
        if state == "ABORTED" and reason is None:
            reason, source = "unexpected error", "system"
        if source is None and reason:
            source = "revert"
        checks = monitor.snapshot() if monitor else []
        impact = None
        if t_inject is not None:
            impact = watchdog.impact(store.query(since=t_inject - 20), base, t_inject, t_revert or time.time())
        _update(
            store, exp_id, state=state, abort_reason=reason, recovery_seconds=recovery,
            finished_at=time.time(), impact=json.dumps(impact) if impact else None,
            recovery_measured=None if t_inject is None else 0 if not_measured else 1,
            abort_source=source if state == "ABORTED" else None, safety_checks=len(checks),
            safety_decisions=json.dumps(checks) if checks else None,
            injection=json.dumps(inj_rec) if inj_rec else None, recovery_agent=json.dumps(ra_rec) if ra_rec else None,
        )
    return get(store, exp_id)

