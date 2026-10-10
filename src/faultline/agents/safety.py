import os
import threading
import time
from typing import Literal

from pydantic import BaseModel, field_validator

from .. import analyzer, llm
from ..baseline import done_ts
from ..stats import percentile
from ..store import Store

REASON_MAX = 200
WINDOW_S = 10.0
_warned = False


class SafetyOut(BaseModel):
    decision: Literal["continue", "abort"]
    reason: str
    concern_level: Literal["low", "medium", "high"]

    @field_validator("decision", "concern_level", mode="before")
    @classmethod
    def _lower(cls, v):
        return str(v).strip().lower()

    @field_validator("reason", mode="before")
    @classmethod
    def _trim(cls, v):
        return analyzer.clean(str(v))[:REASON_MAX]


SYSTEM = (
    "You are the live safety monitor of a chaos engineering tool running a fault on a microservice demo shop. A deterministic "
    "watchdog already aborts when a journey crosses its hard limits; you are an extra layer that may only make things safer. "
    "You get the fault, the hypothesis with its predicted blast radius, the baseline of each user journey, the last 10 s of "
    "measurements, the trend against the 10 s before, and the limits. Answer abort only when the data shows harm beyond what the "
    "hypothesis expects: journeys outside the predicted blast radius degrading, errors spreading, or a clearly worsening trend that "
    "will cross the limits soon. Degradation inside the predicted blast radius that is stable or within limits is expected: answer continue. "
    "Reply with one JSON object and nothing else: "
    '{"decision":"continue|abort","reason":"<=200 chars, cite numbers from the input","concern_level":"low|medium|high"}. '
    "Use plain ASCII punctuation, no em dashes."
)


def context_from_map(graph: dict, target: str, hypothesis: str | None = None, blast: list[str] | None = None) -> dict:
    from .planner import blast_radius

    blast = blast_radius(graph, target) if blast is None else blast
    pred, _ = analyzer.predicted_journeys(graph, target, blast)
    return {"hypothesis": hypothesis, "blast_radius": list(blast), "predicted_journeys": sorted(pred)}


def window_stats(rows, journey, lo, hi) -> dict:
    w = [r for r in rows if r["journey"] == journey and lo < done_ts(r) <= hi]
    n = len(w)
    return {
        "count": n,
        "error_rate": (sum(1 for r in w if not r["ok"]) / n) if n else None,
        "p95_ms": percentile([r["latency_ms"] for r in w], 95),
    }


def _pct(v):
    return "-" if v is None else f"{100 * v:.0f}%"


def _ms(v):
    return "-" if v is None else f"{v:.0f}ms"


def build_input(cfg, base: dict, rows, fault, target, params, elapsed, duration, context, now) -> tuple[str, list[str]]:
    context = context or {}
    predicted = context.get("predicted_journeys")
    lines, outside = [], []
    for j, th in base["thresholds"].items():
        b = base["journeys"][j]
        cur = window_stats(rows, j, now - WINDOW_S, now)
        prev = window_stats(rows, j, now - 2 * WINDOW_S, now - WINDOW_S)
        deg = analyzer.journey_degradation(cfg, {**cur, "base_error_rate": b["error_rate"], "base_p95_ms": b["p95_ms"]}) if cur["count"] else 0.0
        if predicted is not None and j not in predicted and cur["count"] and (deg >= cfg.score.affected_margin or analyzer.breached(cur, th)):
            outside.append(j)
        lines.append(
            f"{j}: baseline err {_pct(b['error_rate'])} p95 {_ms(b['p95_ms'])}; last10s n={cur['count']} err {_pct(cur['error_rate'])} "
            f"p95 {_ms(cur['p95_ms'])}; prev10s n={prev['count']} err {_pct(prev['error_rate'])} p95 {_ms(prev['p95_ms'])}; "
            f"limits err {_pct(th['error_rate_max'])} p95 {_ms(th['p95_max_ms'])}"
        )
    shown = ", ".join(f"{k}={v:g}" for k, v in (params or {}).items() if v) or "none"
    text = (
        f"Fault: {fault} on {target}, params {shown}, elapsed {elapsed:.0f}s of {duration}s\n"
        f"Hypothesis: {context.get('hypothesis') or 'none (manual experiment)'}\n"
        f"Predicted blast radius: {', '.join((context.get('blast_radius') or [])[:12]) or 'none'}\n"
        f"Predicted journeys: {', '.join(predicted) if predicted else 'none'}\n"
        + "\n".join(lines)
        + "\nJourneys degraded outside the predicted blast radius: "
        + (", ".join(outside) if outside else "none" if predicted is not None else "unknown (no prediction)")
    )
    return text, outside


class Monitor:
    """Runs safety checks in a background thread; the watchdog loop only polls abort_reason()."""

    def __init__(self, cfg, db_path, base, fault, target, params, duration, context, t_inject, chat=None, log=print):
        self.cfg, self.db_path, self.base = cfg, db_path, base
        self.fault, self.target, self.params, self.duration = fault, target, params, duration
        self.context, self.t_inject, self.chat, self.log = context, t_inject, chat, log
        self.checks: list[dict] = []
        self._abort: str | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        global _warned
        sa = self.cfg.safety_agent
        if not sa.enabled:
            return False
        if self.chat is None and not os.environ.get("GROQ_API_KEY"):
            if not _warned:
                _warned = True
                self.log("safety agent disabled: no GROQ_API_KEY (watchdog only)")
            return False
        self._thread = threading.Thread(target=self._loop, daemon=True, name="safety-agent")
        self._thread.start()
        return True

    def stop(self):
        self._stop.set()

    def join(self, timeout: float):
        if self._thread:
            self._thread.join(timeout)

    def abort_reason(self) -> str | None:
        with self._lock:
            return self._abort

    def snapshot(self) -> list[dict]:
        with self._lock:
            return list(self.checks)

    def _loop(self):
        sa = self.cfg.safety_agent
        store = Store(self.db_path)
        try:
            nxt = self.t_inject + sa.first_check_s
            while not self._stop.wait(max(0.0, nxt - time.time())):
                if nxt - self.t_inject >= self.duration - 1:
                    break
                self._check(store)
                nxt = max(nxt + sa.interval_s, time.time())
        except Exception:
            pass
        finally:
            store.close()

    def _check(self, store):
        cfg = self.cfg
        now = time.time()
        elapsed = now - self.t_inject
        rows = store.query(since=now - 2 * WINDOW_S - 5)
        user, _ = build_input(cfg, self.base, rows, self.fault, self.target, self.params, elapsed, self.duration, self.context, now)
        timeout = cfg.safety_agent.timeout_s
        out, meta = llm.call_structured(
            "safety", cfg.llm.safety_model, SafetyOut, SYSTEM, user, timeout_s=timeout, store=store,
            input_summary=f"{self.fault} {self.target} t={elapsed:.0f}/{self.duration}s", chat=self.chat, cache=False,
        )
        error = meta["error"]
        if out is not None and meta["latency_ms"] > timeout * 1000:
            out, error = None, "ignored: slower than timeout_s"
            store.add_trace("safety", cfg.llm.safety_model, f"late check t={elapsed:.0f}s", None, meta["latency_ms"], True, error)
        rec = {
            "t": round(elapsed, 1), "decision": out.decision if out else None, "concern": out.concern_level if out else None,
            "reason": out.reason if out else None, "latency_ms": round(meta["latency_ms"]),
            "tokens": meta["prompt_tokens"] + meta["completion_tokens"], "ignored": out is None, "error": error,
        }
        with self._lock:
            self.checks.append(rec)
            if out is not None and out.decision == "abort" and not self._stop.is_set() and self._abort is None:
                self._abort = out.reason
        self.log(
            f"safety check t={rec['t']:g}s: " + (f"{rec['decision']} ({rec['concern']}) {rec['reason']}" if out else f"ignored ({error})")
            + f" [{rec['latency_ms']} ms, {rec['tokens']} tok]"
        )


def summarize(checks: list[dict]) -> dict:
    return {
        "checks": len(checks),
        "continue": sum(1 for c in checks if c.get("decision") == "continue"),
        "abort": sum(1 for c in checks if c.get("decision") == "abort"),
        "ignored": sum(1 for c in checks if c.get("ignored")),
    }


def describe(checks: list[dict]) -> str:
    if not checks:
        return "-"
    s = summarize(checks)
    return f"{s['checks']} checks: {s['continue']} continue, {s['abort']} abort, {s['ignored']} ignored"
