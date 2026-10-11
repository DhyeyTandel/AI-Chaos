import pytest

from faultline.config import load_config
from faultline.store import Store


@pytest.fixture(autouse=True)
def fresh_tpm_limiters():
    from faultline import llm

    llm.reset_tpm_limiters()
    yield
    llm.reset_tpm_limiters()


@pytest.fixture
def cfg():
    return load_config(path="/nonexistent")


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    yield s
    s.close()


class FakeDriver:
    def __init__(self):
        self.running = {}
        self.applied = set()
        self.reverts = 0
        self.fail_revert = 0
        self.owned = []
        self.unverified = set()
        self.states = {}
        self.events = []
        self.restarted = []
        self.checked = []

    def check_applied(self, entry, spike_fraction=0.8):
        self.checked.append(entry["kind"])
        ok = entry["id"] in self.applied and entry["kind"] not in self.unverified
        return ok, f"fake {entry['kind']} {'in effect' if ok else 'not in effect'}"

    def check_reverted(self, entry):
        return entry["id"] not in self.applied, "fake revert check"

    def container_states(self, names):
        return [{"name": n, **{"state": "running", "health": "none", "restarts": 0, "oom": False}, **self.states.get(n, {})} for n in names]

    def recent_events(self, names, since, limit=20):
        return list(self.events)

    def restart_container(self, name):
        self.restarted.append(name)

    def apply(self, entry):
        self.applied.add(entry["id"])
        return {**entry["revert"], "restart_policy": {"Name": "unless-stopped"}}

    def revert(self, entry):
        self.reverts += 1
        if self.fail_revert > 0:
            self.fail_revert -= 1
            raise RuntimeError("boom")
        self.applied.discard(entry["id"])

    def verify(self, entry):
        return entry["id"] not in self.applied

    def list_owned(self):
        return list(self.owned)

    def remove_owned(self, ledger_id, target):
        self.owned = [o for o in self.owned if o["ledger_id"] != ledger_id]


@pytest.fixture
def driver():
    return FakeDriver()
