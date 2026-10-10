import asyncio
import json
import secrets
import signal
import threading
import time
from pathlib import Path

from . import baseline as bl
from . import experiment as exp_mod
from . import watchdog
from .prober import run_probe

ROUND_COLS = (
    "round", "tier", "candidate_id", "fault", "target", "params", "llm_rank", "reason", "fallback", "hypothesis",
    "predicted_blast_radius", "experiment_id", "outcome", "abort_reason", "recovery_s", "climb_decision",
)


def new_run_id() -> str:
    return "run-" + time.strftime("%H%M%S") + "-" + secrets.token_hex(2)


def start(store, run_id: str, rounds: int):
    with store.conn:
        store.conn.execute("INSERT INTO runs (run_id, started_at, rounds_planned) VALUES (?, ?, ?)", (run_id, time.time(), rounds))


def update(store, run_id: str, **cols):
    keys = ", ".join(f"{k} = ?" for k in cols)
    with store.conn:
        store.conn.execute(f"UPDATE runs SET {keys} WHERE run_id = ?", (*cols.values(), run_id))


def add_round(store, run_id: str, row: dict):
    vals = [json.dumps(row[c]) if c in ("params", "predicted_blast_radius") else row[c] for c in ROUND_COLS]
    with store.conn:
        store.conn.execute(
            f"INSERT INTO run_rounds (run_id, {', '.join(ROUND_COLS)}) VALUES ({', '.join('?' * (len(ROUND_COLS) + 1))})",
            (run_id, *vals),
        )


def get(store, run_id: str | None = None) -> dict | None:
    if run_id:
        r = store.conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    else:
        r = store.conn.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()
    return dict(r) if r else None


def rounds(store, run_id: str) -> list[dict]:
    out = []
    for r in store.conn.execute("SELECT * FROM run_rounds WHERE run_id = ? ORDER BY round", (run_id,)):
        r = dict(r)
        r["params"] = json.loads(r["params"]) if r["params"] else {}
        r["predicted_blast_radius"] = json.loads(r["predicted_blast_radius"]) if r["predicted_blast_radius"] else []
        out.append(r)
    return out


class RunControl:
    """Run-level kill switch: STOP sentinel (watched in a thread so no LLM call can miss it), signals, or request()."""

    def __init__(self, path=None, poll_s: float = 0.25):
        self.path = Path(path) if path else exp_mod.stop_path()
        self.poll_s = poll_s
        self._reason: str | None = None
        self._done = threading.Event()
        self._thread: threading.Thread | None = None

    def request(self, reason: str):
        self._reason = self._reason or reason

    def reason(self) -> str | None:
        if self._reason is None and self.path.exists():
            self._reason = "kill switch (STOP file)"
        return self._reason

    def install_signals(self):
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, lambda *_: self.request("operator interrupt (signal)"))
            except ValueError:
                pass

    def __enter__(self):
        def watch():
            while not self._done.wait(self.poll_s):
                self.reason()

        self._thread = threading.Thread(target=watch, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._done.set()
        if self._thread:
            self._thread.join(1)


def is_operator_stop(abort_reason: str | None) -> bool:
    r = abort_reason or ""
    return r.startswith(("kill switch", "operator"))


async def _wait(store, cfg, thresholds, max_s: float, control) -> tuple[str, str]:
    t0 = time.time()
    stop = asyncio.Event()
    task = asyncio.create_task(run_probe(store, max_s, timeout=cfg.watchdog.probe_timeout_s, stop=stop))
    detail = "insufficient samples"
    try:
        while time.time() - t0 < max_s:
            await asyncio.sleep(1.0)
            if control.reason():
                return "stopped", control.reason()
            now = time.time()
            breaches, _, short = watchdog.judge(
                store.query(since=t0), thresholds, now, cfg.preflight.probe_s, cfg.watchdog.min_samples, since=t0
            )
            if not breaches and not short:
                return "ok", ""
            detail = "; ".join(breaches) or f"insufficient samples for {', '.join(short)}"
        return "timeout", detail
    finally:
        stop.set()
        await asyncio.gather(task, return_exceptions=True)


def wait_for_baseline(store, cfg, control) -> tuple[str, str]:
    """Probe until a short window is within baseline thresholds. Returns (ok | timeout | stopped, detail)."""
    base = bl.latest(store)
    if base is None:
        return "timeout", "no baseline"
    return asyncio.run(_wait(store, cfg, base["thresholds"], cfg.run.settle_wait_s, control))
