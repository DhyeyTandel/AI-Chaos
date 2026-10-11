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
    impact TEXT,
    abort_source TEXT,
    safety_checks INTEGER NOT NULL DEFAULT 0,
    safety_decisions TEXT
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
    error TEXT,
    prompt_tokens INTEGER,
    completion_tokens INTEGER
);

CREATE TABLE IF NOT EXISTS llm_usage (
    day TEXT NOT NULL,
    model TEXT NOT NULL,
    requests INTEGER NOT NULL DEFAULT 0,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, model)
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    started_at REAL NOT NULL,
    ended_at REAL,
    rounds_planned INTEGER NOT NULL,
    rounds_done INTEGER NOT NULL DEFAULT 0,
    final_tier INTEGER,
    stop_reason TEXT
);

CREATE TABLE IF NOT EXISTS run_rounds (
    run_id TEXT NOT NULL,
    round INTEGER NOT NULL,
    tier INTEGER NOT NULL,
    candidate_id TEXT,
    fault TEXT,
    target TEXT,
    params TEXT,
    llm_rank INTEGER,
    reason TEXT,
    fallback INTEGER NOT NULL DEFAULT 0,
    hypothesis TEXT,
    predicted_blast_radius TEXT,
    experiment_id TEXT,
    outcome TEXT,
    abort_reason TEXT,
    recovery_s REAL,
    climb_decision TEXT,
    PRIMARY KEY (run_id, round)
);

CREATE TABLE IF NOT EXISTS fixes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    weakness_rank INTEGER,
    target TEXT NOT NULL,
    change TEXT NOT NULL,
    original TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at REAL NOT NULL,
    applied_at REAL,
    reverted_at REAL,
    error TEXT,
    before_experiment_ids TEXT,
    after_experiment_ids TEXT,
    before_penalty REAL,
    after_penalty REAL,
    verdict TEXT,
    details TEXT
);

CREATE TABLE IF NOT EXISTS recovery_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id TEXT NOT NULL,
    step INTEGER NOT NULL,
    action TEXT NOT NULL,
    service TEXT,
    reason TEXT,
    diagnosis TEXT,
    result TEXT,
    ts REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS llm_cache (
    key TEXT PRIMARY KEY,
    model TEXT NOT NULL,
    created_at REAL NOT NULL,
    output_json TEXT NOT NULL
);
"""

MIGRATIONS = {
    "agent_trace": {"prompt_tokens": "INTEGER", "completion_tokens": "INTEGER"},
    "experiments": {
        "abort_source": "TEXT", "safety_checks": "INTEGER NOT NULL DEFAULT 0", "safety_decisions": "TEXT",
        "preflight_review": "TEXT", "recovery_measured": "INTEGER", "injection": "TEXT", "recovery_agent": "TEXT",
    },
    "runs": {"memory": "TEXT"},
    "run_rounds": {"preflight": "TEXT"},
}


def today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


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
        self._migrate()

    def _migrate(self):
        for table, cols in MIGRATIONS.items():
            have = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for col, typ in cols.items():
                if col not in have:
                    try:
                        self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
                    except sqlite3.OperationalError:
                        pass
        self.conn.commit()

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

    def add_trace(self, agent, model, input_summary, output_json, latency_ms, fallback, error=None, prompt_tokens=None, completion_tokens=None):
        try:
            with self.conn:
                self.conn.execute(
                    "INSERT INTO agent_trace (ts, agent, model, input_summary, output_json, latency_ms, fallback, error,"
                    " prompt_tokens, completion_tokens) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (time.time(), agent, model, input_summary, output_json, latency_ms, int(bool(fallback)), error,
                     prompt_tokens, completion_tokens),
                )
        except sqlite3.Error:
            pass

    def add_recovery_action(self, experiment_id, step, action, service, reason, diagnosis, result):
        with self.conn:
            self.conn.execute(
                "INSERT INTO recovery_actions (experiment_id, step, action, service, reason, diagnosis, result, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (experiment_id, step, action, service, reason, diagnosis, result, time.time()),
            )

    def recovery_actions(self, experiment_id: str) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM recovery_actions WHERE experiment_id = ? ORDER BY step, id", (experiment_id,))
        return [dict(r) for r in rows]

    def traces(self, limit: int = 20):
        rows = self.conn.execute("SELECT * FROM agent_trace ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows][::-1]

    def add_usage(self, model, requests, prompt_tokens, completion_tokens):
        try:
            with self.conn:
                self.conn.execute(
                    "INSERT INTO llm_usage (day, model, requests, prompt_tokens, completion_tokens) VALUES (?, ?, ?, ?, ?)"
                    " ON CONFLICT(day, model) DO UPDATE SET requests = requests + excluded.requests,"
                    " prompt_tokens = prompt_tokens + excluded.prompt_tokens,"
                    " completion_tokens = completion_tokens + excluded.completion_tokens",
                    (today(), model, requests, prompt_tokens, completion_tokens),
                )
        except sqlite3.Error:
            pass

    def usage_today(self, model: str) -> dict:
        r = self.conn.execute(
            "SELECT requests, prompt_tokens, completion_tokens FROM llm_usage WHERE day = ? AND model = ?", (today(), model)
        ).fetchone()
        req, pt, ct = (r["requests"], r["prompt_tokens"], r["completion_tokens"]) if r else (0, 0, 0)
        return {"requests": req, "prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct}

    def usage_all_today(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM llm_usage WHERE day = ? ORDER BY model", (today(),))
        return [dict(r) for r in rows]

    def cache_get(self, key: str, ttl_s: float):
        r = self.conn.execute("SELECT output_json FROM llm_cache WHERE key = ? AND created_at >= ?", (key, time.time() - ttl_s)).fetchone()
        return r["output_json"] if r else None

    def cache_put(self, key: str, model: str, output_json: str):
        try:
            with self.conn:
                self.conn.execute(
                    "INSERT OR REPLACE INTO llm_cache (key, model, created_at, output_json) VALUES (?, ?, ?, ?)",
                    (key, model, time.time(), output_json),
                )
        except sqlite3.Error:
            pass

    def cache_drop(self, key: str):
        try:
            with self.conn:
                self.conn.execute("DELETE FROM llm_cache WHERE key = ?", (key,))
        except sqlite3.Error:
            pass

    def cache_hits_today(self) -> int:
        start = time.time() // 86400 * 86400
        r = self.conn.execute("SELECT COUNT(*) AS n FROM agent_trace WHERE error = 'cache' AND ts >= ?", (start,)).fetchone()
        return r["n"]

    def close(self):
        self.conn.close()
