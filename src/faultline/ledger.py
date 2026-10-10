import json
import os
import time

PENDING, APPLIED, REVERTED, REVERT_FAILED = "PENDING", "APPLIED", "REVERTED", "REVERT_FAILED"
OPEN_STATES = (PENDING, APPLIED, REVERT_FAILED)
LEASE_S = 30


def _row(r):
    d = dict(r)
    d["params"] = json.loads(d["params"])
    d["revert"] = json.loads(d["revert"])
    return d


class Ledger:
    def __init__(self, store):
        self.conn = store.conn

    def create(self, experiment_id, kind, target, params, revert, expires_at) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO ledger (experiment_id, kind, target, params, revert, state, created_at, expires_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (experiment_id, kind, target, json.dumps(params), json.dumps(revert), PENDING, time.time(), expires_at),
            )
        return cur.lastrowid

    def get(self, entry_id: int):
        r = self.conn.execute("SELECT * FROM ledger WHERE id = ?", (entry_id,)).fetchone()
        return _row(r) if r else None

    def all(self):
        return [_row(r) for r in self.conn.execute("SELECT * FROM ledger ORDER BY id")]

    def open_entries(self):
        q = ",".join("?" * len(OPEN_STATES))
        return [_row(r) for r in self.conn.execute(f"SELECT * FROM ledger WHERE state IN ({q}) ORDER BY id", OPEN_STATES)]

    def mark_applied(self, entry_id: int, revert: dict | None = None):
        with self.conn:
            if revert is not None:
                self.conn.execute("UPDATE ledger SET revert = ? WHERE id = ?", (json.dumps(revert), entry_id))
            self.conn.execute(
                "UPDATE ledger SET state = ?, applied_at = ? WHERE id = ? AND state = ?",
                (APPLIED, time.time(), entry_id, PENDING),
            )

    def claim(self, entry_id: int, lease_s: float = LEASE_S) -> bool:
        now = time.time()
        with self.conn:
            cur = self.conn.execute(
                "UPDATE ledger SET lease_until = ? WHERE id = ? AND state != ?"
                " AND (lease_until IS NULL OR lease_until < ?)",
                (now + lease_s, entry_id, REVERTED, now),
            )
        return cur.rowcount == 1

    def finish(self, entry_id: int, ok: bool, error: str | None = None):
        with self.conn:
            if ok:
                self.conn.execute(
                    "UPDATE ledger SET state = ?, reverted_at = ?, error = NULL, lease_until = NULL"
                    " WHERE id = ? AND state != ?",
                    (REVERTED, time.time(), entry_id, REVERTED),
                )
            else:
                self.conn.execute(
                    "UPDATE ledger SET state = ?, error = ?, lease_until = NULL WHERE id = ? AND state != ?",
                    (REVERT_FAILED, error, entry_id, REVERTED),
                )

    def beat(self, experiment_id: str | None):
        with self.conn:
            self.conn.execute(
                "INSERT INTO heartbeat (id, ts, experiment_id, pid) VALUES (1, ?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET ts = excluded.ts, experiment_id = excluded.experiment_id,"
                " pid = excluded.pid",
                (time.time(), experiment_id, os.getpid()),
            )

    def heartbeat_ts(self) -> float | None:
        r = self.conn.execute("SELECT ts FROM heartbeat WHERE id = 1").fetchone()
        return r["ts"] if r else None
