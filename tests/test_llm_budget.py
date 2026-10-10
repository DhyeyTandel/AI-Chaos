import sqlite3
import time

import pytest
from pydantic import BaseModel

from faultline import llm
from faultline.store import Store


class Out(BaseModel):
    x: int


class Msg:
    def __init__(self, content, usage=None):
        self.content = content
        self.usage_metadata = usage


class Chat:
    def __init__(self, usage=None, content='{"x": 1}'):
        self.calls, self.usage, self.content = 0, usage, content

    def bind(self, **kw):
        return self

    def invoke(self, msgs):
        self.calls += 1
        return Msg(self.content, self.usage)


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.delenv("FAULTLINE_LLM_CACHE", raising=False)
    llm.reset_limiter(llm.TokenBucket(1000))
    yield
    llm.reset_limiter(None)


def call(store, chat, user="u", timeout=1):
    return llm.call_structured("planner", "m", Out, "s", user, timeout, store=store, chat=chat)


def test_token_bucket_blocks_when_exhausted_and_refills():
    t = [0.0]
    b = llm.TokenBucket(600, clock=lambda: t[0], sleep=lambda s: t.__setitem__(0, t[0] + s))
    assert b.try_acquire(500)
    assert not b.try_acquire(200)
    assert not b.acquire(0.5, 200)
    assert b.acquire(15.0, 200)
    b.adjust(-1000)
    assert not b.try_acquire(1)


def test_call_blocked_when_tokens_exhausted(store):
    llm.tpm_limiter("m", 100).adjust(-100)
    chat = Chat()
    out, meta = call(store, chat, timeout=0.05)
    assert out is None and "token bucket" in meta["error"] and chat.calls == 0


def test_daily_budget_refusal_falls_back(store):
    store.add_usage("m", 3, 100000, 90000)
    chat = Chat()
    out, meta = call(store, chat)
    assert out is None and meta["error"] == "daily token budget reached" and chat.calls == 0
    row = store.traces(1)[0]
    assert row["fallback"] == 1 and row["error"] == "daily token budget reached"
    assert store.usage_today("other")["total_tokens"] == 0


def test_usage_reconciliation_updates_llm_usage_and_trace(store):
    chat = Chat(usage={"input_tokens": 700, "output_tokens": 300, "total_tokens": 1000})
    out, meta = call(store, chat)
    assert out.x == 1
    u = store.usage_today("m")
    assert u == {"requests": 1, "prompt_tokens": 700, "completion_tokens": 300, "total_tokens": 1000}
    row = store.traces(1)[0]
    assert (row["prompt_tokens"], row["completion_tokens"]) == (700, 300)
    bucket = llm.tpm_limiter("m")
    assert bucket.tokens == pytest.approx(bucket.capacity - 1000, abs=5)


def test_cache_hit_skips_client_and_budget(store):
    chat = Chat(usage={"input_tokens": 10, "output_tokens": 5})
    call(store, chat)
    before = llm.tpm_limiter("m").tokens
    reqs = llm.limiter().tokens
    out, meta = call(store, chat)
    assert out.x == 1 and chat.calls == 1 and meta["cached"] and not meta["fallback"]
    assert llm.tpm_limiter("m").tokens >= before and llm.limiter().tokens >= reqs
    assert store.usage_today("m")["requests"] == 1
    row = store.traces(1)[0]
    assert row["error"] == "cache" and row["fallback"] == 0
    assert store.cache_hits_today() == 1
    store.add_usage("m", 1, 10**6, 0)
    assert call(store, chat)[1]["cached"]


def test_cache_disabled_and_expired(store, monkeypatch):
    chat = Chat()
    monkeypatch.setenv("FAULTLINE_LLM_CACHE", "0")
    call(store, chat)
    call(store, chat)
    assert chat.calls == 2
    monkeypatch.setenv("FAULTLINE_LLM_CACHE", "1")
    call(store, chat, user="v")
    store.conn.execute("UPDATE llm_cache SET created_at = ?", (time.time() - 10**6,))
    call(store, chat, user="v")
    assert chat.calls == 4


def test_cache_key_varies_with_inputs():
    k = llm.cache_key("m", "low", "s", "u", Out)
    assert k != llm.cache_key("m", "medium", "s", "u", Out)
    assert k != llm.cache_key("m2", "low", "s", "u", Out)
    assert k != llm.cache_key("m", "low", "s", "u2", Out)


def test_get_chat_passes_reasoning_effort(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "x")
    c = llm.get_chat("openai/gpt-oss-120b", 5, "low", 1500)
    assert c.reasoning_effort == "low" and c.max_tokens == 1500
    assert llm.get_chat("llama-3.1-8b-instant", 5, "low").reasoning_effort is None


def test_migrates_old_agent_trace(tmp_path):
    path = str(tmp_path / "old.db")
    c = sqlite3.connect(path)
    c.execute(
        "CREATE TABLE agent_trace (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, agent TEXT NOT NULL, model TEXT,"
        " input_summary TEXT, output_json TEXT, latency_ms REAL, fallback INTEGER NOT NULL DEFAULT 0, error TEXT)"
    )
    c.execute("INSERT INTO agent_trace (ts, agent, fallback) VALUES (1.0, 'planner', 0)")
    c.commit()
    c.close()
    s = Store(path)
    Store(path).close()
    cols = {r["name"] for r in s.conn.execute("PRAGMA table_info(agent_trace)")}
    assert {"prompt_tokens", "completion_tokens"} <= cols
    s.add_trace("planner", "m", "i", None, 1.0, False, None, 5, 6)
    rows = s.traces(5)
    assert rows[0]["prompt_tokens"] is None and rows[1]["completion_tokens"] == 6
    s.close()
