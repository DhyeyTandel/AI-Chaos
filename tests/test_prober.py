import asyncio

import httpx

from faultline.prober import Prober, run_probe
from faultline.store import Store


def _client(handler):
    return httpx.AsyncClient(base_url="http://t", transport=httpx.MockTransport(handler))


def test_timeout_recorded_as_failure():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    async def go():
        async with _client(handler) as c:
            return await Prober(c, timeout=0.5).tick()

    samples = asyncio.run(go())
    assert [s.journey for s in samples] == ["home", "product", "cart", "checkout"]
    assert all(not s.ok and "ReadTimeout" in s.error for s in samples)


def test_success_and_http_error():
    def handler(request):
        if request.url.path == "/api/checkout":
            return httpx.Response(500, json={"error": "x"})
        return httpx.Response(200, json={})

    async def go():
        async with _client(handler) as c:
            return await Prober(c).tick()

    by = {s.journey: s for s in asyncio.run(go())}
    assert by["home"].ok and by["product"].ok and by["cart"].ok
    assert not by["checkout"].ok and by["checkout"].status == 500


def test_product_journey_requests_recommendations():
    seen = []

    def handler(request):
        seen.append((request.url.path, dict(request.url.params)))
        return httpx.Response(200, json={})

    async def go():
        async with _client(handler) as c:
            return await Prober(c).run_journey("product")

    assert asyncio.run(go()).ok
    assert [p for p, _ in seen] == ["/api/products/OLJCESPC7Z", "/api/recommendations"]
    assert seen[1][1] == {"productIds": "OLJCESPC7Z", "sessionId": "faultline-prober", "currencyCode": "USD"}


def test_recommendation_failure_fails_product_journey():
    def handler(request):
        return httpx.Response(503 if request.url.path == "/api/recommendations" else 200, json={})

    async def go():
        async with _client(handler) as c:
            return await Prober(c).run_journey("product")

    s = asyncio.run(go())
    assert not s.ok and s.status == 503 and "recommendations" in s.error


def test_run_probe_stores_samples():
    store = Store(":memory:")
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    asyncio.run(run_probe(store, 1.5, base_url="http://t", transport=transport))
    assert len(store.query(journey="home")) >= 1
