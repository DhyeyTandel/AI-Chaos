import json
import time

from .. import analyzer, faults
from ..injector import Injector
from ..ledger import Ledger
from ..store import Store

AGENT = "injection"
MODEL = "deterministic"


class NotVerified(RuntimeError):
    pass


def prepare(cfg, fault, target, params, duration) -> dict:
    """The approved (possibly reduced) plan as an injection spec: validated parameters and the parts in apply order."""
    params = faults.validate(cfg, fault, target, duration, params)
    parts = params["parts"] if fault == faults.COMBO else [{"fault": fault, "target": target, "params": params}]
    return {
        "fault": fault, "target": target, "duration_s": duration, "params": params,
        "parts": [{"fault": p["fault"], "target": p["target"], "kind": analyzer.fault_kind(p["fault"], p["params"]), "label": analyzer.part_label(p)} for p in parts],
    }


def verify(cfg, driver, entries, sleep=time.sleep, clock=time.time) -> list[dict]:
    """Poll each applied part until it is seen in effect or its timeout passes."""
    c = cfg.injection
    out = []
    for e in entries:
        kind = analyzer.fault_kind(e["kind"], e["params"])
        end = clock() + (c.spike_timeout_s if kind == "spike" else c.verify_timeout_s)
        while True:
            try:
                ok, detail = driver.check_applied(e, c.spike_fraction)
            except Exception as ex:
                ok, detail = False, f"{type(ex).__name__}: {str(ex)[:80]}"
            if ok or clock() >= end:
                break
            sleep(c.poll_s)
        out.append({"ledger_id": e["id"], "part": analyzer.part_label({"fault": e["kind"], "target": e["target"], "params": e["params"]}), "kind": kind, "verified": bool(ok), "detail": detail})
    return out


def inject(db_path, driver, cfg, exp_id: str, spec: dict, sleep=time.sleep) -> dict:
    """Ledger write-ahead and apply in safe order (Injector), then verify every part. On a failed verification only this
    experiment's own entries are reverted and NotVerified is raised."""
    t0 = time.monotonic()
    store = Store(db_path)
    summary = f"{spec['fault']} {spec['target']}, parts: " + "; ".join(p["label"] for p in spec["parts"])
    try:
        inj = Injector(Ledger(store), driver, cfg)
        try:
            if spec["fault"] == faults.COMBO:
                ids = inj.inject_combo(exp_id, spec["params"], spec["duration_s"])
            else:
                ids = [inj.inject(exp_id, spec["fault"], spec["target"], spec["params"], spec["duration_s"])]
        except Exception as e:
            store.add_trace(AGENT, MODEL, summary, None, (time.monotonic() - t0) * 1000, False, f"apply failed: {type(e).__name__}: {str(e)[:120]}")
            raise
        ledger = Ledger(store)
        checks = verify(cfg, driver, [ledger.get(i) for i in ids], sleep)
        bad = [c for c in checks if not c["verified"]]
        rec = {"verified": not bad, "parts": checks}
        reason = "; ".join(f"{c['part']}: {c['detail']}" for c in bad)
        if bad:
            inj.revert_experiment(exp_id)
        store.add_trace(AGENT, MODEL, summary, json.dumps(rec), (time.monotonic() - t0) * 1000, False, reason or None)
        if bad:
            err = NotVerified(reason)
            err.record = rec
            raise err
        return rec
    finally:
        store.close()


def describe(rec: dict | None) -> str:
    if not rec:
        return "Injection agent: no record"
    ok = [c["part"] for c in rec["parts"] if c["verified"]]
    bad = [f"{c['part']} ({c['detail']})" for c in rec["parts"] if not c["verified"]]
    if bad:
        return "Injection agent: NOT verified " + "; ".join(bad) + (f"; verified {', '.join(ok)}" if ok else "")
    return "Injection agent: verified " + ", ".join(ok)
