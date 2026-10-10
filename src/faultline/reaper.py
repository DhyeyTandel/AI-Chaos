import time

from .injector import Injector
from .ledger import Ledger


def tick(ledger: Ledger, injector: Injector, cfg, now: float | None = None) -> dict[int, str]:
    now = now or time.time()
    entries = ledger.open_entries()
    results: dict[int, str] = {}
    if entries:
        hb = ledger.heartbeat_ts() or 0.0
        newest = max(e["created_at"] for e in entries)
        stale = now - max(hb, newest) > cfg.reaper.stale_s
        for e in entries:
            if stale or e["expires_at"] < now:
                state = injector.revert(e["id"], retries=2)
                if state:
                    results[e["id"]] = state
    injector.sweep_orphans()
    return results


def run(store, driver, cfg, log=print, stop=lambda: False):
    ledger = Ledger(store)
    injector = Injector(ledger, driver, cfg)
    log("reaper started")
    last: dict[int, str] = {}
    while not stop():
        try:
            for i, state in tick(ledger, injector, cfg).items():
                if last.get(i) != state:
                    log(f"reaper: ledger {i} -> {state}")
                last[i] = state
        except Exception as e:
            log(f"reaper error: {type(e).__name__}: {e}")
        time.sleep(cfg.reaper.interval_s)
