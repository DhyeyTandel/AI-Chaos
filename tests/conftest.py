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
