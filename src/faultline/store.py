import os
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS probe_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_wall REAL NOT NULL,
    ts_mono REAL NOT NULL,
    journey TEXT NOT NULL,
    ok INTEGER NOT NULL,
    status INTEGER,
    latency_ms REAL NOT NULL,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_samples_journey_ts ON probe_samples (journey, ts_wall);

CREATE TABLE IF NOT EXISTS baselines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    params TEXT NOT NULL,
    data TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id TEXT,
    kind TEXT NOT NULL,
    target TEXT NOT NULL,
    params TEXT NOT NULL,
    revert TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at REAL NOT NULL,
    applied_at REAL,
    expires_at REAL NOT NULL,
    reverted_at REAL,
    error TEXT,
    lease_until REAL
);

CREATE TABLE IF NOT EXISTS heartbeat (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    ts REAL NOT NULL,
    experiment_id TEXT,
    pid INTEGER
);

CREATE TABLE IF NOT EXISTS experiments (
    id TEXT PRIMARY KEY,
    fault TEXT NOT NULL,
    target TEXT NOT NULL,
    params TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at REAL NOT NULL,
    injected_at REAL,
    reverted_at REAL,
    finished_at REAL,
    abort_reason TEXT,
    recovery_seconds REAL,
    impact TEXT
);

CREATE TABLE IF NOT EXISTS agent_trace (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    agent TEXT NOT NULL,
    model TEXT,
    input_summary TEXT,
    output_json TEXT,
    latency_ms REAL,
    fallback INTEGER NOT NULL DEFAULT 0,
    error TEXT
);
"""


def default_db_path() -> str:
    return os.environ.get("FAULTLINE_DB", "/data/faultline.db")


class Store:
    def __init__(self, path: str | None = None):
        self.path = str(path or default_db_path())
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)

    def insert_sample(self, ts_wall, ts_mono, journey, ok, status, latency_ms, error=None):
        self.insert_many([(ts_wall, ts_mono, journey, ok, status, latency_ms, error)])

    def insert_many(self, rows):
        self.conn.executemany(
            "INSERT INTO probe_samples (ts_wall, ts_mono, journey, ok, status, latency_ms, error)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(w, m, j, int(bool(o)), st, lat, e) for w, m, j, o, st, lat, e in rows],
        )
        self.conn.commit()

    def query(self, journey: str | None = None, since: float | None = None, until: float | None = None):
        sql = "SELECT * FROM probe_samples WHERE 1=1"
        args: list = []
        if journey is not None:
            sql += " AND journey = ?"
            args.append(journey)
        if since is not None:
            sql += " AND ts_wall >= ?"
            args.append(since)
        if until is not None:
            sql += " AND ts_wall <= ?"
            args.append(until)
        sql += " ORDER BY ts_wall, id"
        return [dict(r) for r in self.conn.execute(sql, args)]

    def add_trace(self, agent, model, input_summary, output_json, latency_ms, fallback, error=None):
        try:
            with self.conn:
                self.conn.execute(
                    "INSERT INTO agent_trace (ts, agent, model, input_summary, output_json, latency_ms, fallback, error)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (time.time(), agent, model, input_summary, output_json, latency_ms, int(bool(fallback)), error),
                )
        except sqlite3.Error:
            pass

    def traces(self, limit: int = 20):
        rows = self.conn.execute("SELECT * FROM agent_trace ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows][::-1]

    def close(self):
        self.conn.close()
