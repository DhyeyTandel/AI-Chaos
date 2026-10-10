import time

import docker
from docker.errors import APIError, NotFound

FAULTS = ("crash", "delay")
NEVER = frozenset(
    {"faultline", "reaper", "prober", "otel-collector", "prometheus", "jaeger", "load-generator"}
)
NETSHOOT_IMAGE = "nicolaka/netshoot:v0.13"
OWNED_LABEL = "faultline.owned"
ID_LABEL = "faultline.ledger_id"
ROLE_LABEL = "faultline.role"
EXPIRES_LABEL = "faultline.expires_at"
TARGET_LABEL = "faultline.target"

DETECT_IF = (
    "IF=$(ip -o -4 route show default | awk '{print $5; exit}'); "
    "[ -n \"$IF\" ] || IF=$(ip -o link show | awk -F': ' '$2!=\"lo\"{sub(/@.*/,\"\",$2); print $2; exit}'); "
)
NETEM_SCRIPT = (
    DETECT_IF
    + "clear() { tc qdisc show dev \"$IF\" | grep -q netem && tc qdisc del dev \"$IF\" root; true; }; "
    "trap 'clear; exit 0' TERM INT; "
    "tc qdisc replace dev \"$IF\" root netem delay ${DELAY_MS}ms ${JITTER_MS}ms loss ${LOSS_PCT}% || exit 1; "
    "sleep \"$TTL\" & wait $!; clear"
)
CLEAR_SCRIPT = (
    DETECT_IF
    + "tc qdisc show dev \"$IF\" | grep -q netem && tc qdisc del dev \"$IF\" root; true"
)
SHOW_SCRIPT = DETECT_IF + "tc qdisc show dev \"$IF\""


class ValidationError(ValueError):
    pass


def validate(cfg, fault: str, target: str, duration: float, params: dict) -> dict:
    s = cfg.safety
    if fault not in FAULTS:
        raise ValidationError(f"unknown fault {fault!r}; allowed: {', '.join(FAULTS)}")
    if target in NEVER or target not in s.allow:
        raise ValidationError(f"target {target!r} is not in the allowlist")
    if fault == "crash" and target in s.crash_exclude:
        raise ValidationError(f"target {target!r} may not be crashed")
    if not 1 <= duration <= s.max_duration_s:
        raise ValidationError(f"duration must be 1..{s.max_duration_s} seconds")
    out = {"duration_s": duration}
    if fault == "delay":
        d = float(params.get("delay_ms", 200))
        j = float(params.get("jitter_ms", 0))
        loss = float(params.get("loss_pct", 0))
        if not 1 <= d <= s.max_delay_ms:
            raise ValidationError(f"delay_ms must be 1..{s.max_delay_ms}")
        if not 0 <= j <= s.max_jitter_ms:
            raise ValidationError(f"jitter_ms must be 0..{s.max_jitter_ms}")
        if not 0 <= loss <= s.max_loss_pct:
            raise ValidationError(f"loss_pct must be 0..{s.max_loss_pct}")
        out.update(delay_ms=d, jitter_ms=j, loss_pct=loss, ttl_s=duration + s.expiry_margin_s)
    return out


def initial_revert(fault: str, target: str) -> dict:
    return {"action": "start_container" if fault == "crash" else "clear_netem", "target": target}


class DockerDriver:
    def __init__(self, client=None):
        self.client = client or docker.from_env()

    def _target(self, name):
        return self.client.containers.get(name)

    def is_running(self, name) -> bool:
        try:
            c = self._target(name)
            c.reload()
            return c.status == "running"
        except NotFound:
            return False

    def apply(self, entry: dict) -> dict:
        kind, target = entry["kind"], entry["target"]
        if kind == "crash":
            c = self._target(target)
            policy = c.attrs["HostConfig"].get("RestartPolicy") or {"Name": "", "MaximumRetryCount": 0}
            c.update(restart_policy={"Name": "no"})
            c.kill()
            return {**entry["revert"], "restart_policy": policy}
        if kind == "delay":
            self._ensure_image()
            p = entry["params"]
            self._target(target)
            self.client.containers.run(
                NETSHOOT_IMAGE,
                ["sh", "-c", NETEM_SCRIPT],
                name=f"faultline-netem-{entry['id']}",
                detach=True,
                network_mode=f"container:{target}",
                cap_add=["NET_ADMIN"],
                environment={
                    "DELAY_MS": f"{p['delay_ms']:g}",
                    "JITTER_MS": f"{p['jitter_ms']:g}",
                    "LOSS_PCT": f"{p['loss_pct']:g}",
                    "TTL": f"{int(p['ttl_s'])}",
                },
                labels={
                    OWNED_LABEL: "true",
                    ROLE_LABEL: "netem",
                    ID_LABEL: str(entry["id"]),
                    TARGET_LABEL: target,
                    EXPIRES_LABEL: f"{entry['expires_at']:.0f}",
                },
            )
            self._wait_netem(target, entry["id"])
            return entry["revert"]
        raise ValueError(f"unknown fault kind {kind}")

    def revert(self, entry: dict):
        kind, target = entry["kind"], entry["target"]
        if kind == "crash":
            try:
                c = self._target(target)
            except NotFound:
                raise RuntimeError(f"container {target} not found")
            pol = entry["revert"].get("restart_policy")
            if pol is not None:
                c.update(restart_policy={"Name": pol.get("Name") or "no", "MaximumRetryCount": pol.get("MaximumRetryCount", 0)})
            c.reload()
            if c.status != "running":
                c.start()
        elif kind == "delay":
            self._clear_netem(target)
            self._remove_sidecars(entry["id"])
        else:
            raise ValueError(f"unknown fault kind {kind}")

    def verify(self, entry: dict) -> bool:
        kind, target = entry["kind"], entry["target"]
        if kind == "crash":
            return self.is_running(target)
        if kind == "delay":
            return not self._netem_active(target) and not self._sidecars(entry["id"])
        return False

    def list_owned(self) -> list[dict]:
        out = []
        for c in self.client.containers.list(all=True, filters={"label": f"{OWNED_LABEL}=true"}):
            lb = c.labels or {}
            out.append({"name": c.name, "ledger_id": int(lb.get(ID_LABEL, 0) or 0), "target": lb.get(TARGET_LABEL, "")})
        return out

    def remove_owned(self, ledger_id: int, target: str):
        self._clear_netem(target)
        self._remove_sidecars(ledger_id)

    def _ensure_image(self):
        try:
            self.client.images.get(NETSHOOT_IMAGE)
        except NotFound:
            self.client.images.pull(NETSHOOT_IMAGE)

    def _sidecars(self, ledger_id):
        return self.client.containers.list(all=True, filters={"label": f"{ID_LABEL}={ledger_id}"})

    def _remove_sidecars(self, ledger_id):
        for c in self._sidecars(ledger_id):
            try:
                c.remove(force=True)
            except NotFound:
                pass

    def _netshoot(self, target, script) -> str:
        out = self.client.containers.run(
            NETSHOOT_IMAGE,
            ["sh", "-c", script],
            network_mode=f"container:{target}",
            cap_add=["NET_ADMIN"],
            remove=True,
            labels={OWNED_LABEL: "true", ROLE_LABEL: "oneshot", TARGET_LABEL: target},
        )
        return out.decode(errors="replace")

    def _clear_netem(self, target):
        if not self.is_running(target):
            return
        self._ensure_image()
        try:
            self._netshoot(target, CLEAR_SCRIPT)
        except APIError:
            if self.is_running(target):
                raise

    def _netem_active(self, target) -> bool:
        if not self.is_running(target):
            return False
        return "netem" in self._netshoot(target, SHOW_SCRIPT)

    def _wait_netem(self, target, ledger_id, timeout=15.0):
        end = time.time() + timeout
        while time.time() < end:
            cs = self._sidecars(ledger_id)
            if cs and cs[0].status == "exited":
                raise RuntimeError("netem sidecar exited: " + cs[0].logs(tail=5).decode(errors="replace").strip())
            if cs and cs[0].status == "running":
                code, out = cs[0].exec_run(["sh", "-c", SHOW_SCRIPT])
                if b"netem" in out:
                    return
            time.sleep(0.3)
        raise RuntimeError("netem did not become active")
