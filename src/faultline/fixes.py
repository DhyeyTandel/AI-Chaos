"""Fixer auto-apply and verification.

A fix is not a fault: it lives in its own `fixes` table, never in the fault ledger. The reaper and `faultline stop` revert faults only and
never touch a fix; `faultline fix --revert` and `--revert-all` restore the original HostConfig values stored before the change was made.
Only docker `update` on a running container is used (restart policy, memory limit, CPU quota), never `docker compose`."""

import json
import re
import time

import yaml

from . import analyzer, baseline as bl, faults
from .agents.fixer import RESTART
from .ledger import Ledger

PENDING, APPLIED, REVERTED, REVERT_FAILED = "PENDING", "APPLIED", "REVERTED", "REVERT_FAILED"
OPEN_STATES = (PENDING, APPLIED, REVERT_FAILED)
IMPROVED, NO_CHANGE, WORSE, INCONCLUSIVE = "IMPROVED", "NO CHANGE", "WORSE", "INCONCLUSIVE"
MB = 1024 * 1024
DEFAULT_PERIOD = 100000
SIZE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kmg]?)i?b?\s*$", re.I)
UNITS = {"": 1, "k": 1024, "m": MB, "g": 1024 * MB}
PROJECT_LABEL = "com.docker.compose.project"
SERVICE_LABEL = "com.docker.compose.service"
ONEOFF_LABEL = "com.docker.compose.oneoff"


class Refused(ValueError):
    pass


def parse_size(v) -> int | None:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return int(v)
    m = SIZE.match(str(v))
    return int(float(m.group(1)) * UNITS[m.group(2).lower()]) if m else None


def parse_restart(v) -> dict | None:
    s = str(v).strip()
    if not RESTART.match(s):
        return None
    name, _, n = s.partition(":")
    return {"Name": name, "MaximumRetryCount": int(n) if n else 0}


def applicable(cfg, fix) -> tuple[dict | None, str | None]:
    """(change, None) when the fix maps to a restart policy and/or a raised memory or CPU limit on one allowlisted service, within the
    [fix] caps. Otherwise (None, reason) and the fix stays a proposal. `change` also lists the keys that stay propose-only."""
    f = fix.model_dump() if hasattr(fix, "model_dump") else dict(fix)
    if f.get("fix_type") != "config":
        return None, f"{f.get('fix_type')} change: propose-only"
    if not f.get("compose_override"):
        return None, "no compose override: propose-only"
    try:
        doc = yaml.safe_load(f["compose_override"])
        services = doc["services"]
    except Exception:
        return None, "override is not a services mapping: propose-only"
    if not isinstance(services, dict) or len(services) != 1:
        return None, "override must name exactly one service: propose-only"
    (svc, body), = services.items()
    if svc in faults.NEVER or svc not in cfg.safety.allow:
        return None, f"{svc} is not an allowlisted target: propose-only"
    if not isinstance(body, dict):
        return None, "override body is not a mapping: propose-only"
    change: dict = {"service": svc}
    skipped = []
    if "restart" in body:
        r = parse_restart(body["restart"])
        if r is None:
            return None, "bad restart policy: propose-only"
        change["restart"] = r
    for k in body:
        if k not in ("restart", "deploy"):
            skipped.append(k)
    limits = ((body.get("deploy") or {}).get("resources") or {}).get("limits") if isinstance(body.get("deploy"), dict) else None
    if isinstance(body.get("deploy"), dict):
        res = body["deploy"].get("resources") or {}
        if res.get("reservations"):
            skipped.append("deploy.resources.reservations")
    for k, v in (limits or {}).items():
        if k == "memory":
            b = parse_size(v)
            if b is None or b <= 0:
                return None, "unreadable memory limit: propose-only"
            if b > cfg.fix.max_memory_mb * MB:
                return None, f"memory {b / MB:g} MB exceeds max_memory_mb {cfg.fix.max_memory_mb:g}: propose-only"
            change["memory_bytes"] = b
        elif k == "cpus":
            try:
                c = float(v)
            except (TypeError, ValueError):
                return None, "unreadable cpus limit: propose-only"
            if c <= 0:
                return None, "unreadable cpus limit: propose-only"
            if c > cfg.fix.max_cpus:
                return None, f"cpus {c:g} exceeds max_cpus {cfg.fix.max_cpus:g}: propose-only"
            change["cpus"] = c
        else:
            skipped.append(f"deploy.resources.limits.{k}")
    if not {"restart", "memory_bytes", "cpus"} & set(change):
        return None, "only " + (", ".join(skipped) or "nothing") + ": propose-only"
    change["skipped"] = skipped
    return change, None


def applicability_text(cfg, fix) -> str:
    change, why = applicable(cfg, fix)
    if change is None:
        return f"Propose-only ({why})."
    skipped = f"; propose-only part: {', '.join(change['skipped'])}" if change["skipped"] else ""
    return f"Auto-applicable with `faultline fix` ({describe_change(change)}){skipped}."


def describe_change(change: dict) -> str:
    bits = []
    if "restart" in change:
        r = change["restart"]
        bits.append(f"restart {r['Name']}" + (f":{r['MaximumRetryCount']}" if r.get("MaximumRetryCount") else ""))
    if "memory_bytes" in change:
        bits.append(f"memory {change['memory_bytes'] / MB:g} MB")
    if "cpus" in change:
        bits.append(f"cpus {change['cpus']:g}")
    return f"{change['service']}: " + ", ".join(bits)


def live_plan(change: dict, hc: dict) -> tuple[dict, dict, list[str]]:
    """(update kwargs, original values of every field that changes, human lines) from the container's current HostConfig.
    Limits can only be raised and only where one exists: docker update cannot remove a limit again, so anything else is refused."""
    kw: dict = {}
    orig: dict = {}
    lines = []
    r = change.get("restart")
    if r:
        cur = hc.get("RestartPolicy") or {}
        name, n = cur.get("Name") or "no", cur.get("MaximumRetryCount") or 0
        if (name, n) != (r["Name"], r["MaximumRetryCount"]):
            orig["restart"] = {"Name": name, "MaximumRetryCount": n}
            kw["restart_policy"] = dict(r)
            lines.append(f"restart policy {name} -> {r['Name']}")
    skipped = []
    m = change.get("memory_bytes")
    if m:
        cur, swap = hc.get("Memory") or 0, hc.get("MemorySwap") or 0
        if cur <= 0:
            skipped.append("memory: no existing limit, and a limit cannot be removed again through docker update")
        elif m < cur:
            skipped.append(f"memory: {m / MB:g} MB would lower the current limit {cur / MB:g} MB")
        elif m > cur:
            orig["memory"] = {"Memory": cur, "MemorySwap": swap}
            kw["mem_limit"] = m
            if swap > 0:
                kw["memswap_limit"] = int(m * swap / cur)
            lines.append(f"memory limit {cur / MB:g} MB -> {m / MB:g} MB")
    c = change.get("cpus")
    if c:
        q, period, nano = hc.get("CpuQuota") or 0, hc.get("CpuPeriod") or DEFAULT_PERIOD, hc.get("NanoCpus") or 0
        cur = q / period
        if nano > 0:
            skipped.append("cpus: the limit is set as NanoCpus, which docker update cannot change")
        elif q <= 0:
            skipped.append("cpus: no existing limit, and a limit cannot be removed again through docker update")
        elif c < cur:
            skipped.append(f"cpus: {c:g} would lower the current limit {cur:g}")
        elif c > cur:
            orig["cpu"] = {"CpuQuota": q}
            kw["cpu_quota"] = int(c * period)
            lines.append(f"cpu limit {cur:g} -> {c:g}")
    if not kw:
        raise Refused("; ".join(skipped) or "already in effect; nothing to change")
    lines += [f"not applied, {x}" for x in skipped]
    return kw, orig, lines


def revert_kwargs(original: dict) -> dict:
    kw: dict = {}
    if "restart" in original:
        kw["restart_policy"] = dict(original["restart"])
    if "memory" in original:
        kw["mem_limit"] = original["memory"]["Memory"]
        if original["memory"]["MemorySwap"]:
            kw["memswap_limit"] = original["memory"]["MemorySwap"]
    if "cpu" in original:
        kw["cpu_quota"] = original["cpu"]["CpuQuota"]
    return kw


def matches(original: dict, hc: dict) -> bool:
    """True when the HostConfig holds the values of `original` (restart policy name 'no' and '' are the same)."""
    if "restart" in original:
        cur = hc.get("RestartPolicy") or {}
        if ((cur.get("Name") or "no"), cur.get("MaximumRetryCount") or 0) != (original["restart"]["Name"], original["restart"]["MaximumRetryCount"]):
            return False
    if "memory" in original:
        if (hc.get("Memory") or 0) != original["memory"]["Memory"]:
            return False
        if original["memory"]["MemorySwap"] and (hc.get("MemorySwap") or 0) != original["memory"]["MemorySwap"]:
            return False
    if "cpu" in original and (hc.get("CpuQuota") or 0) != original["cpu"]["CpuQuota"]:
        return False
    return True


def wanted_matches(kw: dict, hc: dict) -> bool:
    cur = hc.get("RestartPolicy") or {}
    if "restart_policy" in kw and ((cur.get("Name") or "no"), cur.get("MaximumRetryCount") or 0) != (kw["restart_policy"]["Name"], kw["restart_policy"]["MaximumRetryCount"]):
        return False
    if "mem_limit" in kw and (hc.get("Memory") or 0) != kw["mem_limit"]:
        return False
    if "memswap_limit" in kw and (hc.get("MemorySwap") or 0) != kw["memswap_limit"]:
        return False
    return not ("cpu_quota" in kw and (hc.get("CpuQuota") or 0) != kw["cpu_quota"])


def _row(r):
    d = dict(r)
    for k in ("change", "original", "details", "before_experiment_ids", "after_experiment_ids"):
        d[k] = json.loads(d[k]) if d.get(k) else ([] if k.endswith("_ids") else None)
    return d


class Fixes:
    def __init__(self, store):
        self.conn = store.conn

    def create(self, run_id, rank, target, change, original, before_ids) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO fixes (run_id, weakness_rank, target, change, original, state, created_at, before_experiment_ids)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, rank, target, json.dumps(change), json.dumps(original), PENDING, time.time(), json.dumps(before_ids)),
            )
        return cur.lastrowid

    def get(self, fix_id: int):
        r = self.conn.execute("SELECT * FROM fixes WHERE id = ?", (fix_id,)).fetchone()
        return _row(r) if r else None

    def all(self):
        return [_row(r) for r in self.conn.execute("SELECT * FROM fixes ORDER BY id")]

    def open(self):
        q = ",".join("?" * len(OPEN_STATES))
        return [_row(r) for r in self.conn.execute(f"SELECT * FROM fixes WHERE state IN ({q}) ORDER BY id", OPEN_STATES)]

    def set(self, fix_id: int, **cols):
        keys = ", ".join(f"{k} = ?" for k in cols)
        vals = [json.dumps(v) if k in ("details", "after_experiment_ids") else v for k, v in cols.items()]
        with self.conn:
            self.conn.execute(f"UPDATE fixes SET {keys} WHERE id = ?", (*vals, fix_id))


class Applier:
    """Applies and reverts fixes on running containers through the Docker API."""

    def __init__(self, store, cfg, client):
        self.store, self.cfg, self.client = store, cfg, client
        self.fixes = Fixes(store)

    def container(self, service: str):
        found = self.client.containers.list(all=True, filters={"label": [f"{PROJECT_LABEL}=ai-chaos", f"{SERVICE_LABEL}={service}"]})
        found = [c for c in found if (c.labels or {}).get(ONEOFF_LABEL) != "True"]
        if not found:
            raise Refused(f"no container for service {service}")
        running = [c for c in found if c.status == "running"]
        if not running:
            raise Refused(f"{service} is not running")
        return running[0]

    def preview(self, change: dict) -> tuple[dict, list[str]]:
        c = self.container(change["service"])
        c.reload()
        kw, _, lines = live_plan(change, c.attrs["HostConfig"])
        return kw, lines

    def apply(self, change: dict, run_id, rank, before_ids) -> int:
        if Ledger(self.store).open_entries():
            raise Refused("open fault ledger entries exist; run `faultline stop` first")
        svc = change["service"]
        if any(f["target"] == svc for f in self.fixes.open()):
            raise Refused(f"{svc} already has an unreverted fix; run `faultline fix --revert-all`")
        c = self.container(svc)
        c.reload()
        kw, original, _ = live_plan(change, c.attrs["HostConfig"])
        fix_id = self.fixes.create(run_id, rank, svc, change, {"container": c.name, **original}, before_ids)
        try:
            c.update(**kw)
            c.reload()
            if not wanted_matches(kw, c.attrs["HostConfig"]):
                raise RuntimeError("docker did not apply the requested values")
        except Exception as e:
            state = self.revert(fix_id)
            self.fixes.set(fix_id, error=f"apply failed: {type(e).__name__}: {e}"[:300])
            raise Refused(f"apply failed ({type(e).__name__}: {e}); original values {'restored' if state == REVERTED else 'NOT restored'}")
        self.fixes.set(fix_id, state=APPLIED, applied_at=time.time())
        return fix_id

    def revert(self, fix_id: int) -> str:
        f = self.fixes.get(fix_id)
        if f is None or f["state"] == REVERTED:
            return REVERTED
        original = {k: v for k, v in f["original"].items() if k != "container"}
        err = None
        try:
            if original:
                c = self.container(f["target"])
                c.update(**revert_kwargs(original))
                c.reload()
                if not matches(original, c.attrs["HostConfig"]):
                    err = "original values not restored"
        except Exception as e:
            err = f"{type(e).__name__}: {e}"[:300]
        if err is None:
            self.fixes.set(fix_id, state=REVERTED, reverted_at=time.time(), error=None)
            return REVERTED
        self.fixes.set(fix_id, state=REVERT_FAILED, error=err)
        return REVERT_FAILED

    def revert_all(self) -> dict[int, str]:
        return {f["id"]: self.revert(f["id"]) for f in self.fixes.open()}

    def wait_stable(self, service: str, settle_s: float, timeout_s: float, sleep=time.sleep, now=time.time) -> tuple[bool, str]:
        """Running, not restarted, not OOM killed and healthy (when a healthcheck exists) for settle_s in a row."""
        c = self.container(service)
        c.reload()
        restarts = c.attrs.get("RestartCount", 0)
        end = now() + timeout_s
        ok_since = None
        detail = "timeout"
        while now() < end:
            c.reload()
            st = c.attrs.get("State", {})
            health = (st.get("Health") or {}).get("Status")
            if c.attrs.get("RestartCount", 0) != restarts or st.get("OOMKilled"):
                return False, "container restarted or was OOM killed after the change"
            good = st.get("Status") == "running" and health in (None, "healthy")
            detail = f"state {st.get('Status')} health {health or 'none'}"
            if good:
                ok_since = ok_since if ok_since is not None else now()
                if now() - ok_since >= settle_s:
                    return True, detail
            else:
                ok_since = None
            sleep(1.0)
        return False, detail


def verdict(cfg, before: list[float], after: list[float]) -> str:
    """Mean penalty before vs after the fix, compared with [fix] improve_min_penalty."""
    if not before or len(after) != len(before):
        return INCONCLUSIVE
    delta = sum(before) / len(before) - sum(after) / len(after)
    t = cfg.fix.improve_min_penalty
    return IMPROVED if delta >= t else WORSE if delta <= -t else NO_CHANGE


def penalty_of(cfg, store, exp: dict, graph: dict) -> float:
    base = bl.latest(store)
    return analyzer.analyze_experiment(cfg, exp, None, graph, base["thresholds"] if base else None)["penalty"]


def rerun_args(exp: dict) -> tuple[str, str, int, dict]:
    p = dict(exp["params"])
    return exp["fault"], exp["target"], int(p.get("duration_s") or 20), p


def comparable(before: dict, after: dict) -> str | None:
    """None when the after run is a valid repeat of the before run, else why it is not."""
    if after["state"] == "VETOED":
        return "pre-flight review vetoed the re-run"
    if after["injected_at"] is None:
        return f"re-run did not inject ({after['abort_source'] or after['state']}: {after['abort_reason']})"
    if after["state"] == "ABORTED" and after["abort_source"] not in ("watchdog", "safety_agent"):
        return f"re-run aborted by {after['abort_source']}"
    keys = ("users", "spawn_rate", "delay_ms", "jitter_ms", "loss_pct", "duration_s", "parts")
    if any(before["params"].get(k) != after["params"].get(k) for k in keys):
        return "pre-flight review changed the re-run parameters"
    return None


def journey_rows(before: dict, after: dict) -> dict:
    out = {}
    for j, b in (before.get("impact") or {}).items():
        a = (after.get("impact") or {}).get(j) or {}
        out[j] = {"p95_before": b.get("p95_ms"), "p95_after": a.get("p95_ms"), "err_before": b.get("error_rate"), "err_after": a.get("error_rate")}
    return out


def run_fix(store, cfg, applier: Applier, change: dict, befores: list[dict], rerun, graph: dict, run_id=None, rank=None, log=print) -> dict:
    """Apply, wait for a stable container, re-run the same experiments, score, decide keep or revert. Any error or interrupt reverts."""
    before_pen = [penalty_of(cfg, store, e, graph) for e in befores]
    fix_id = applier.apply(change, run_id, rank, [e["id"] for e in befores])
    fixes = applier.fixes
    decided = False
    try:
        fixes.set(fix_id, before_penalty=sum(before_pen) / len(before_pen))
        log(f"fix #{fix_id} applied to {change['service']}; waiting for it to be stable")
        ok, detail = applier.wait_stable(change["service"], cfg.fix.settle_s, cfg.fix.stable_timeout_s)
        afters, why, pairs, after_pen = [], None, [], []
        if not ok:
            why = f"container not stable after the change ({detail})"
        else:
            for b in befores:
                a = rerun(b)
                afters.append(a)
                why = comparable(b, a)
                if why:
                    break
                pairs.append((b, a))
                after_pen.append(penalty_of(cfg, store, a, graph))
        v = INCONCLUSIVE if why else verdict(cfg, before_pen, after_pen)
        keep = v == IMPROVED or (not cfg.fix.keep_if_improved and v == NO_CHANGE)
        details = {
            "reason": why,
            "pairs": [
                {"before": b["id"], "after": a["id"], "before_penalty": before_pen[i], "after_penalty": after_pen[i],
                 "journeys": journey_rows(b, a), "after_outcome": a["state"]}
                for i, (b, a) in enumerate(pairs)
            ],
        }
        fixes.set(
            fix_id, after_experiment_ids=[a["id"] for a in afters], after_penalty=(sum(after_pen) / len(after_pen)) if after_pen and not why else None,
            verdict=v, details=details,
        )
        decided = True
        state = APPLIED
        if not keep:
            state = applier.revert(fix_id)
        log(f"verdict {v}: fix #{fix_id} {'kept' if keep else 'reverted' if state == REVERTED else 'REVERT FAILED'}")
        return fixes.get(fix_id)
    finally:
        if not decided:
            applier.revert(fix_id)
            fixes.set(fix_id, verdict=INCONCLUSIVE)


def report_rows(store, analysis: dict) -> list[dict]:
    """Fixes whose before experiments are part of the analysis, with the analyzed before and after experiments."""
    by = {a["experiment_id"]: a for a in analysis["experiments"]}
    after = analysis.get("after_fix") or {}
    out = []
    for f in Fixes(store).all():
        if f["state"] == PENDING:
            continue
        if not any(i in by for i in f["before_experiment_ids"]):
            continue
        out.append({**f, "before": [by[i] for i in f["before_experiment_ids"] if i in by], "after": [after[i] for i in f["after_experiment_ids"] if i in after]})
    return out
