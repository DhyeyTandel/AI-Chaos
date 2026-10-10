import time

from . import faults
from .ledger import REVERT_FAILED, REVERTED, Ledger


class Injector:
    def __init__(self, ledger: Ledger, driver, cfg, retry_delay: float = 1.0):
        self.ledger = ledger
        self.driver = driver
        self.cfg = cfg
        self.retry_delay = retry_delay

    def inject(self, experiment_id, fault, target, params, duration) -> int:
        params = faults.validate(self.cfg, fault, target, duration, params)
        if self.ledger.open_entries():
            raise faults.ValidationError("active faults exist in the ledger; run `faultline stop`")
        expires = time.time() + duration + self.cfg.safety.expiry_margin_s
        entry_id = self.ledger.create(experiment_id, fault, target, params, faults.initial_revert(fault, target), expires)
        entry = self.ledger.get(entry_id)
        try:
            snapshot = getattr(self.driver, "snapshot", None)
            pre = snapshot(entry) if snapshot else None
            if pre is not None:
                self.ledger.set_revert(entry_id, pre)
                entry = self.ledger.get(entry_id)
            revert = self.driver.apply(entry)
        except Exception:
            self.revert(entry_id)
            raise
        self.ledger.mark_applied(entry_id, revert)
        return entry_id

    def revert(self, entry_id: int, retries: int = 3) -> str | None:
        entry = self.ledger.get(entry_id)
        if entry is None or entry["state"] == REVERTED:
            return REVERTED
        if not self.ledger.claim(entry_id):
            return None
        err = None
        for attempt in range(retries):
            try:
                self.driver.revert(entry)
                if self.driver.verify(entry):
                    err = None
                    break
                err = "verify failed after revert"
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
            if attempt + 1 < retries:
                time.sleep(self.retry_delay)
        self.ledger.finish(entry_id, err is None, err)
        return REVERTED if err is None else REVERT_FAILED

    def revert_all(self, wait_s: float = 30.0) -> dict[int, str]:
        """Global revert: kill switch and reaper only. An experiment must use revert_experiment."""
        return self._revert_open(wait_s, None)

    def revert_experiment(self, experiment_id: str, wait_s: float = 30.0) -> dict[int, str]:
        return self._revert_open(wait_s, experiment_id)

    def _revert_open(self, wait_s: float, experiment_id: str | None) -> dict[int, str]:
        results: dict[int, str] = {}
        deadline = time.time() + wait_s
        while True:
            busy = False
            for e in self.ledger.open_entries():
                if experiment_id is not None and e["experiment_id"] != experiment_id:
                    continue
                if e["id"] in results:
                    continue
                state = self.revert(e["id"])
                if state is None:
                    busy = True
                else:
                    results[e["id"]] = state
            if not busy or time.time() >= deadline:
                break
            time.sleep(0.5)
        self.sweep_orphans()
        return results

    def sweep_orphans(self) -> list[str]:
        live = {e["id"] for e in self.ledger.open_entries()}
        swept = []
        for c in self.driver.list_owned():
            if c["ledger_id"] and c["ledger_id"] not in live:
                try:
                    self.driver.remove_owned(c["ledger_id"], c["target"])
                    swept.append(c["name"])
                except Exception:
                    pass
        return swept
