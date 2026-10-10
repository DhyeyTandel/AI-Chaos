import os
import sqlite3
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
"""


def default_db_path() -> str:
    return os.environ.get("FAULTLINE_DB", "/data/faultline.db")


class Store:
    def __init__(self, path: str | None = None):
        self.path = str(path or default_db_path())
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    def insert_sample(self, ts_wall, ts_mono, journey, ok, status, latency_ms, error=None):
        self.conn.execute(
            "INSERT INTO probe_samples (ts_wall, ts_mono, journey, ok, status, latency_ms, error)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ts_wall, ts_mono, journey, int(bool(ok)), status, latency_ms, error),
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

    def close(self):
        self.conn.close()
