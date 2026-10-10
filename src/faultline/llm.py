import hashlib
import json
import os
import re
import threading
import time

from pydantic import BaseModel, ValidationError

from .config import load_config


class TokenBucket:
    def __init__(self, rate_per_min: float, capacity: float | None = None, clock=time.monotonic, sleep=time.sleep):
        self.rate = rate_per_min / 60.0
        self.capacity = float(capacity if capacity is not None else max(1.0, rate_per_min))
        self.tokens = self.capacity
        self.clock, self.sleep = clock, sleep
        self.last = clock()
        self.lock = threading.Lock()

    def _refill(self):
        now = self.clock()
        self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
        self.last = now

    def try_acquire(self, n: float = 1) -> bool:
        n = min(n, self.capacity)
        with self.lock:
            self._refill()
            if self.tokens >= n:
                self.tokens -= n
                return True
            return False

    def adjust(self, delta: float) -> None:
        with self.lock:
            self._refill()
            self.tokens = max(-self.capacity, min(self.capacity, self.tokens + delta))

    def acquire(self, max_wait_s: float, n: float = 1) -> bool:
        deadline = self.clock() + max_wait_s
        while True:
            if self.try_acquire(n):
                return True
            if self.clock() >= deadline:
                return False
            self.sleep(min(0.25, max(0.01, deadline - self.clock())))


_bucket: TokenBucket | None = None
_bucket_lock = threading.Lock()


def limiter(rate_per_min: float | None = None) -> TokenBucket:
    global _bucket
    with _bucket_lock:
        if _bucket is None:
            _bucket = TokenBucket(rate_per_min if rate_per_min is not None else load_config().llm.rate_per_min)
        return _bucket


def reset_limiter(bucket: TokenBucket | None = None) -> None:
    global _bucket
    with _bucket_lock:
        _bucket = bucket


_tpm_buckets: dict[str, TokenBucket] = {}


def tpm_limiter(model: str, tpm: float | None = None) -> TokenBucket:
    with _bucket_lock:
        if model not in _tpm_buckets:
            _tpm_buckets[model] = TokenBucket(tpm if tpm is not None else load_config().llm.tpm_per_model)
        return _tpm_buckets[model]


def reset_tpm_limiters() -> None:
    with _bucket_lock:
        _tpm_buckets.clear()


def get_chat(model: str, timeout_s: float | None = None, effort: str | None = None, max_tokens: int | None = None):
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        return None
    try:
        from langchain_groq import ChatGroq

        kw = {}
        if effort and model.startswith("openai/gpt-oss"):
            kw["reasoning_effort"] = effort
        if max_tokens:
            kw["max_tokens"] = max_tokens
        return ChatGroq(model=model, api_key=key, temperature=0, timeout=timeout_s or load_config().llm.timeout_s, max_retries=0, **kw)
    except Exception:
        return None


def cache_enabled() -> bool:
    return os.environ.get("FAULTLINE_LLM_CACHE", "1").lower() not in ("0", "false", "no", "off", "")


def cache_key(model, effort, system, user, schema) -> str:
    return hashlib.sha256(json.dumps([model, effort, system, user, schema.__name__]).encode()).hexdigest()


def estimate_tokens(text: str) -> int:
    return len(text) // 4 + 1


def _usage(resp, prompt_est: int, content: str) -> tuple[int, int]:
    um = getattr(resp, "usage_metadata", None) or {}
    if um.get("input_tokens") is not None:
        return int(um["input_tokens"]), int(um.get("output_tokens") or 0)
    tu = (getattr(resp, "response_metadata", None) or {}).get("token_usage") or {}
    if tu.get("prompt_tokens") is not None:
        return int(tu["prompt_tokens"]), int(tu.get("completion_tokens") or 0)
    return prompt_est, estimate_tokens(content)


def _extract_json(text: str) -> str:
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        return m.group(1).strip()
    a, b = text.find("{"), text.rfind("}")
    return text[a : b + 1] if a != -1 and b > a else text


def _retry_after(e: Exception) -> float | None:
    if getattr(e, "status_code", None) != 429:
        return None
    headers = getattr(getattr(e, "response", None), "headers", None) or {}
    try:
        return float(headers.get("retry-after"))
    except (TypeError, ValueError):
        return float("inf")


def _is_timeout(e: Exception) -> bool:
    return "timeout" in type(e).__name__.lower()


def call_structured(agent, model, schema: type[BaseModel], system, user, timeout_s=None, store=None, input_summary=None, chat=None, cache=True):
    """Returns (parsed | None, meta). Never raises. Every call is written to agent_trace when a store is given."""
    t0 = time.monotonic()
    meta = {
        "agent": agent, "model": model, "fallback": True, "error": None, "latency_ms": 0.0, "attempts": 0,
        "cached": False, "prompt_tokens": 0, "completion_tokens": 0,
    }
    parsed = None

    def finish():
        meta["latency_ms"] = (time.monotonic() - t0) * 1000
        meta["fallback"] = parsed is None
        if store is not None:
            try:
                store.add_trace(
                    agent, model, input_summary or user[:300],
                    parsed.model_dump_json() if parsed is not None else None,
                    meta["latency_ms"], parsed is None, meta["error"],
                    meta["prompt_tokens"] or None, meta["completion_tokens"] or None,
                )
            except Exception:
                pass
        return parsed, meta

    try:
        cfg = load_config()
        timeout_s = timeout_s or cfg.llm.timeout_s
        effort = getattr(cfg.llm.reasoning_effort, agent, None)
        max_tokens = getattr(cfg.llm.max_tokens, agent, None)
        key = cache_key(model, effort, system, user, schema)
        use_cache = cache and store is not None and cache_enabled()
        if use_cache:
            hit = store.cache_get(key, cfg.llm.cache_ttl_s)
            if hit is not None:
                try:
                    parsed = schema.model_validate_json(hit)
                    meta["cached"], meta["error"] = True, "cache"
                    return finish()
                except ValidationError:
                    parsed = None
        if store is not None and store.usage_today(model)["total_tokens"] >= cfg.llm.daily_token_budget:
            meta["error"] = "daily token budget reached"
            return finish()
        chat = chat or get_chat(model, timeout_s, effort, max_tokens)
        if chat is None:
            meta["error"] = "no GROQ_API_KEY" if not os.environ.get("GROQ_API_KEY") else "client unavailable"
            return finish()
        bound = chat.bind(response_format={"type": "json_object"})
        msgs = [("system", system), ("user", user)]
        invalid_left, rate_left = 1, 1
        while True:
            prompt_est = estimate_tokens(" ".join(m[1] for m in msgs))
            est = prompt_est + (max_tokens or 0)
            tpm = tpm_limiter(model)
            if not limiter().acquire(timeout_s):
                meta["error"] = "rate limiter: bucket empty"
                return finish()
            if not tpm.acquire(timeout_s, est):
                meta["error"] = "rate limiter: token bucket empty"
                return finish()
            meta["attempts"] += 1
            try:
                resp = bound.invoke(msgs)
            except Exception as e:
                tpm.adjust(min(est, tpm.capacity))
                wait = _retry_after(e)
                if wait is not None and wait <= 10 and rate_left:
                    rate_left -= 1
                    time.sleep(wait)
                    continue
                meta["error"] = "timeout" if _is_timeout(e) else f"{type(e).__name__}: {str(e)[:160]}"
                if wait is not None:
                    meta["error"] = f"429 retry-after {wait:g}s too long" if wait != float("inf") else "429 rate limited"
                return finish()
            content = resp.content if isinstance(getattr(resp, "content", None), str) else str(getattr(resp, "content", resp))
            pt, ct = _usage(resp, prompt_est, content)
            tpm.adjust(min(est, tpm.capacity) - (pt + ct))
            meta["prompt_tokens"] += pt
            meta["completion_tokens"] += ct
            if store is not None:
                store.add_usage(model, 1, pt, ct)
            try:
                parsed = schema.model_validate(json.loads(_extract_json(content)))
                meta["error"] = None
                if use_cache:
                    store.cache_put(key, model, parsed.model_dump_json())
                return finish()
            except (ValidationError, ValueError) as e:
                meta["error"] = f"invalid output: {str(e)[:160]}"
                if invalid_left:
                    invalid_left -= 1
                    msgs = msgs + [("assistant", content[:1500]), ("user", "That was invalid. Reply with only the JSON object in the required format.")]
                    continue
                return finish()
    except Exception as e:
        meta["error"] = f"{type(e).__name__}: {str(e)[:160]}"
        parsed = None
        return finish()
