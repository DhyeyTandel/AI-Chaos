import asyncio
import math
import os
import time
from dataclasses import dataclass

import httpx

JOURNEYS = ("home", "product", "cart", "checkout")
USER_ID = "faultline-prober"
PRODUCT_ID = "OLJCESPC7Z"
CURRENCY = "USD"
DEFAULT_TARGET = "http://frontend-proxy:8080"

CHECKOUT_BODY = {
    "userId": USER_ID,
    "email": "someone@example.com",
    "address": {
        "streetAddress": "1600 Amphitheatre Parkway",
        "state": "CA",
        "country": "United States",
        "city": "Mountain View",
        "zipCode": "94043",
    },
    "userCurrency": CURRENCY,
    "creditCard": {
        "creditCardNumber": "4432-8015-6152-0454",
        "creditCardCvv": 672,
        "creditCardExpirationYear": 2030,
        "creditCardExpirationMonth": 1,
    },
}


@dataclass
class Sample:
    ts_wall: float
    ts_mono: float
    journey: str
    ok: bool
    status: int | None
    latency_ms: float
    error: str | None


class StepError(Exception):
    def __init__(self, msg, status=None):
        super().__init__(msg)
        self.status = status


def target_url() -> str:
    return os.environ.get("FAULTLINE_TARGET", DEFAULT_TARGET).rstrip("/")


class Prober:
    def __init__(self, client: httpx.AsyncClient, timeout: float = 5.0):
        self.client = client
        self.timeout = timeout
        self.sink = None

    async def _req(self, method, path, **kw):
        try:
            r = await asyncio.wait_for(
                self.client.request(method, path, timeout=self.timeout, **kw), self.timeout + 1
            )
        except (httpx.HTTPError, asyncio.TimeoutError) as e:
            raise StepError(f"{method} {path}: {type(e).__name__}") from e
        if r.status_code >= 400:
            raise StepError(f"{method} {path}: HTTP {r.status_code}", r.status_code)
        return r

    async def _add_item(self):
        await self._req(
            "POST",
            "/api/cart",
            params={"currencyCode": CURRENCY},
            json={"item": {"productId": PRODUCT_ID, "quantity": 1}, "userId": USER_ID},
        )

    async def _empty_cart(self):
        await self._req("DELETE", "/api/cart", json={"userId": USER_ID})

    async def _home(self):
        await self._req("GET", "/")
        return await self._req("GET", "/api/products", params={"currencyCode": CURRENCY})

    async def _product(self):
        return await self._req("GET", f"/api/products/{PRODUCT_ID}", params={"currencyCode": CURRENCY})

    async def _cart(self):
        await self._add_item()
        await self._req("GET", "/api/cart", params={"sessionId": USER_ID, "currencyCode": CURRENCY})
        return await self._req("DELETE", "/api/cart", json={"userId": USER_ID})

    async def _checkout(self):
        await self._add_item()
        try:
            return await self._req(
                "POST", "/api/checkout", params={"currencyCode": CURRENCY}, json=CHECKOUT_BODY
            )
        except StepError:
            try:
                await self._empty_cart()
            except StepError:
                pass
            raise

    async def run_journey(self, name: str) -> Sample:
        fn = getattr(self, f"_{name}")
        wall, mono = time.time(), time.monotonic()
        ok, status, error = True, None, None
        try:
            r = await fn()
            status = r.status_code
        except StepError as e:
            ok, status, error = False, e.status, str(e)
        except Exception as e:
            ok, error = False, f"{type(e).__name__}: {e}"
        sample = Sample(wall, mono, name, ok, status, (time.monotonic() - mono) * 1000, error)
        if self.sink:
            self.sink(sample)
        return sample

    async def tick(self) -> list[Sample]:
        async def cart_then_checkout():
            return [await self.run_journey("cart"), await self.run_journey("checkout")]

        res = await asyncio.gather(
            self.run_journey("home"), self.run_journey("product"), cart_then_checkout()
        )
        return [res[0], res[1], *res[2]]


async def run_probe(store, seconds: float, interval: float = 1.0, timeout: float = 5.0,
                    base_url: str | None = None, transport=None, stop: asyncio.Event | None = None):
    stop = stop or asyncio.Event()
    start = time.monotonic()
    nxt = start
    async with httpx.AsyncClient(base_url=base_url or target_url(), transport=transport) as client:
        prober = Prober(client, timeout)
        prober.sink = lambda s: store.insert_sample(
            s.ts_wall, s.ts_mono, s.journey, s.ok, s.status, s.latency_ms, s.error
        )
        while not stop.is_set():
            now = time.monotonic()
            if now - start >= seconds:
                break
            if now < nxt:
                try:
                    await asyncio.wait_for(stop.wait(), nxt - now)
                    break
                except asyncio.TimeoutError:
                    pass
            await prober.tick()
            now = time.monotonic()
            nxt += interval
            if nxt <= now:
                nxt = start + math.ceil((now - start) / interval) * interval
                if nxt <= now:
                    nxt = now + interval
