# Faultline resilience report

| Run | Started | Ended | Rounds | Final tier | Stop reason |
|---|---|---|---|---|---|
| run-142958-f149 | 2026-10-10 14:29:58 UTC | 2026-10-10 14:32:00 UTC | 3/3 | 2 | completed 3 rounds |

**37.9 / 100**  
Overall resilience score (poor). Generated 2026-10-10 14:32:30 UTC.

## Executive summary

The shop's overall resilience score of 37.9 masks several critical weaknesses. The cart service is a clear single point of failure; its crash or cache loss spikes latency to >3 s and aborts experiments. Small latency injections in frontend and astronomy-db cause system-wide watchdog breaches, indicating a wide blast radius. Hidden dependencies surface when astronomy-db delays affect checkout and flagd crashes affect cart, revealing gaps in the dependency map. Finally, even modest delays in the frontend-proxy propagate to most user journeys. Addressing these issues - by adding redundancy to cart,

## Ranked weaknesses and proposed fixes

Fixes are proposals only. Nothing was applied to the running system.

### 1. Cart service single point of failure [CRITICAL]

Services: cart. Experiments: exp-142250-d200, exp-104221-fd4f. Source: llm.

The cart service is a single point of failure. A crash of cart (exp-142250-d200) pushes cart p95 latency to 3007 ms, aborts the experiment and degrades the cart journey completely (impact 1.00). Crashing its cache (valkey-cart, exp-104221-fd4f) produces the same latency spike, confirming that loss of cart stops core checkout flow.

Evidence: exp-142250-d200 crash on cart: p95 3007 ms, impact 1.00, breadth 0.25, penalty 60; exp-104221-fd4f crash on valkey-cart: p95 3007 ms, impact 1.00, breadth 0.25, penalty 60

Proposed fix (architecture, llm): Add Cart Replicas and Restart Policy. Deploy a second instance of the cart service and its Valkey cache. Update the Docker Compose file to run two replicas (docker compose up --scale cart=2 --scale valkey-cart=2). Configure the client library to use a load-balancing round-robin DNS name. Add a circuit-breaker around cart calls that falls back to a stale cache entry if both instances are unavailable.

Compose override dropped by validation: not valid YAML.

Expected effect: If one cart or cache instance crashes, the other continues serving, keeping p95 latency low and preventing full checkout failure.

### 2. Wide blast radius from frontend and astronomy-db latency [HIGH]

Services: frontend, astronomy-db. Experiments: exp-143142-3011, exp-141542-f9bb. Source: llm.

Small latency injections (100-300 ms) on frontend and astronomy-db trigger watchdog breaches across all user journeys (cart, checkout, home, product). Both experiments report impact 1.00 and breadth 1.00, showing these services sit on a critical path whose slowdown propagates system-wide.

Evidence: exp-143142-3011 astronomy-db 300 ms delay: impact 1.00, breadth 1.00, penalty 82; exp-141542-f9bb frontend 100 ms delay: impact 1.00, breadth 1.00, penalty 82

Proposed fix (code, llm): Timeouts, Retries and Circuit Breakers for Frontend & DB. In the frontend and any service calling astronomy-db, set a request timeout of 200 ms. Wrap the call in a retry policy with exponential back-off (max 2 retries). Add a circuit-breaker that opens after 3 consecutive timeouts and returns cached DB data or a graceful degradation response. Deploy a read-through cache for frequent DB queries.

Expected effect: Transient 100-300 ms delays no longer cascade; calls either succeed quickly, use cached data, or fail fast, keeping overall journey impact low.

### 3. Hidden dependencies causing unexpected degradation [HIGH]

Services: checkout, flagd, cart. Experiments: exp-141736-9801, exp-141616-6842. Source: llm.

Experiments reveal services that are not in the predicted journey map but still degrade. A 100 ms delay on astronomy-db (exp-141736-9801) unexpectedly degrades checkout, and a crash of flagd (exp-141616-6842) degrades cart, indicating hidden coupling between these services.

Evidence: exp-141736-9801 astronomy-db delay: degraded checkout (impact 0.83); exp-141616-6842 flagd crash: degraded cart (impact 1.00)

Proposed fix (architecture, llm): Introduce Service-Mesh Resilience for Hidden Couplings. Deploy a lightweight service mesh (e.g., Linkerd). Define per-service policies: 100 ms timeout, 2-retry back-off, and circuit-breaker for calls to flagd and astronomy-db. Enable automatic retries for idempotent GETs. Add observability rules to surface unexpected call graphs.

Expected effect: Crashes or delays in flagd or astronomy-db trigger circuit-breakers and retries instead of silently degrading unrelated services, exposing hidden dependencies.

### 4. Frontend-proxy latency spreads partial impact [MEDIUM]

Services: frontend-proxy. Experiments: exp-142159-53ea. Source: llm.

A 50 ms delay on frontend-proxy (exp-142159-53ea) degrades three of four major journeys (cart, checkout, home) with impact 0.56 and breadth 0.75, showing that even modest slowdown in the proxy layer can affect a large portion of traffic.

Evidence: exp-142159-53ea frontend-proxy 50 ms delay: impact 0.56, breadth 0.75, penalty 52, degraded cart,checkout,home

Proposed fix (config, llm): Scale Frontend-Proxy and Enable Restart. Add a second replica of frontend-proxy in docker-compose (docker compose up --scale frontend-proxy=2). Set restart: unless-stopped for the service. Configure the proxy to use keep-alive connections and enable in-memory response caching for static assets.

Compose override dropped by validation: top level must be services.

Expected effect: A 50 ms delay in one proxy instance is absorbed by the other replica; restart policy ensures quick recovery, reducing impact on cart, checkout and home journeys.

## Experiments in this run

| Round | Tier | Fault | Target | AI reason | Hypothesis | Verdict | Degraded journeys | Impact | Breadth | Recovery | Penalty | Outcome |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 1 | delay 50ms | valkey-cart | Highest detection score; stateful cache, untested path, moderate fan-in reveals hidden dependency on cart/checkout journeys. | Delaying valkey-cart by 50 ms will increase latency of the cart and checkout journeys, exposing the hidden dependency on the stateful cac... | CONFIRMED | cart, checkout | 0.63 | 0.50 | 5.0s | 48 | COMPLETED |
| 2 | 1 | delay 50ms | currency | Targets a non-stateful, low-fan-in service used in all journeys; recent success shows survivability, exposing hidden cross-journey latenc... | Delaying currency by 50 ms will increase latency across all journeys, revealing hidden latency propagation and potential SLA breaches wit... | PARTIAL | checkout | 0.24 | 0.25 | 5.0s | 21 | COMPLETED |
| 3 | 2 | delay 300ms | astronomy-db | Tier-2 light delay on a stateful, fan-in service used by all journeys; reveals hidden dependencies and single-point-of-failure risk after... | Injecting a 300 ms delay into astronomy-db will increase latency across home, product, cart, and checkout journeys, exposing its role as ... | CONFIRMED | cart, checkout, home, product | 1.00 | 1.00 | 5.0s | 82 | ABORTED (watchdog breach: cart p95 1876ms > 800ms; checkout p95 12...) |

## Earlier experiments in the database

These ran before this run and count toward service scores and hidden dependency findings.

| Experiment | Fault | Target | Degraded journeys | Impact | Breadth | Recovery | Penalty | Outcome |
|---|---|---|---|---|---|---|---|---|
| exp-100645-5f9e | loss 10% | recommendation | none | 0.00 | 0.00 | 5.0s | 2 | COMPLETED |
| exp-100938-5e55 | loss 10% | currency | checkout | 0.61 | 0.25 | 5.0s | 40 | COMPLETED |
| exp-101307-fafe | loss 10% | currency | checkout | 0.52 | 0.25 | 5.0s | 35 | COMPLETED |
| exp-101816-a2ca | loss 10% | currency | checkout | 0.60 | 0.25 | 5.0s | 39 | COMPLETED |
| exp-102326-39d7 | loss 10% | currency | checkout | 0.52 | 0.25 | 5.0s | 35 | COMPLETED |
| exp-102927-a733 | loss 10% | currency | checkout | 0.53 | 0.25 | 5.0s | 36 | COMPLETED |
| exp-103908-31bc | loss 10% | currency | checkout | 0.54 | 0.25 | 5.0s | 36 | COMPLETED |
| exp-104221-fd4f | crash | valkey-cart | cart | 1.00 | 0.25 | 6.0s | 60 | ABORTED |
| exp-104625-3f16 | loss 10% | currency | checkout | 0.52 | 0.25 | 5.0s | 35 | COMPLETED |
| exp-105036-fe30 | loss 10% | currency | checkout | 0.52 | 0.25 | 5.0s | 35 | COMPLETED |
| exp-105242-e257 | loss 10% | currency | checkout | 0.51 | 0.25 | 5.0s | 35 | COMPLETED |
| exp-140231-db3a | delay 100ms | quote | checkout | 0.61 | 0.25 | 5.0s | 40 | COMPLETED |
| exp-141542-f9bb | delay 100ms | frontend | cart, checkout, home, product | 1.00 | 1.00 | 5.0s | 82 | ABORTED |
| exp-141616-6842 | crash | flagd | cart | 1.00 | 0.25 | 6.0s | 60 | ABORTED |
| exp-141653-b9e0 | delay 100ms | product-catalog | cart, checkout, home, product | 0.94 | 1.00 | 5.0s | 79 | ABORTED |
| exp-141736-9801 | delay 100ms | astronomy-db | cart, checkout, home, product | 0.83 | 1.00 | 5.0s | 73 | ABORTED |
| exp-142108-4440 | delay 50ms | astronomy-db | cart, checkout | 0.58 | 0.50 | 5.0s | 46 | COMPLETED |
| exp-142159-53ea | delay 50ms | frontend-proxy | cart, checkout, home | 0.56 | 0.75 | 5.0s | 52 | COMPLETED |
| exp-142250-d200 | crash | cart | cart | 1.00 | 0.25 | 6.0s | 60 | ABORTED |

## Hidden dependencies

| Experiment | Scope | Fault | Target | Unexpected journeys | Worst degradation | Map |
|---|---|---|---|---|---|---|
| exp-141616-6842 | history | crash | flagd | cart | 1.00 | map shows no journey depending on it |
| exp-141736-9801 | history | delay 100ms | astronomy-db | checkout | 0.67 | map lists journeys cart, home, product |
| exp-142108-4440 | history | delay 50ms | astronomy-db | checkout | 0.44 | map lists journeys cart, home, product |

## Per-service scores

| Service | Score | Worst experiment | Fault | Experiments | Weight |
|---|---|---|---|---|---|
| astronomy-db | 18.3 | exp-143142-3011 | delay 300ms | 3 | 4 |
| frontend | 18.3 | exp-141542-f9bb | delay 100ms | 1 | 4 |
| product-catalog | 21.4 | exp-141653-b9e0 | delay 100ms | 1 | 4 |
| cart | 40.5 | exp-142250-d200 | crash | 1 | 2 |
| flagd | 40.5 | exp-141616-6842 | crash | 1 | 1 |
| valkey-cart | 40.5 | exp-104221-fd4f | crash | 2 | 2 |
| frontend-proxy | 47.7 | exp-142159-53ea | delay 50ms | 1 | 4 |
| currency | 60.2 | exp-100938-5e55 | loss 10% | 10 | 4 |
| quote | 60.2 | exp-140231-db3a | delay 100ms | 1 | 1 |
| recommendation | 98.3 | exp-100645-5f9e | loss 10% | 1 | 1 |

Untested services (excluded from the overall score): accounting, ad, checkout, email, fraud-detection, image-provider, kafka, payment, shipping

## Score formula

```
penalty = 100 x (0.5 x impact + 0.3 x breadth + 0.2 x recovery)
service score = 100 - worst penalty observed on that service
overall = mean of service scores weighted by journey dependency
```

- impact: worst journey degradation in the fault window. Per journey, max(error part, latency part). Error part = (error rate - baseline error rate) / 0.5, clamped to 0..1. Latency part = ln(p95 / max(baseline p95, 100 ms)) / ln(10), clamped to 0..1.
- breadth: affected journeys / all journeys. A journey is affected when its degradation is at least 0.2 or it breaches the baseline threshold.
- recovery: min(1, recovery seconds / 60); a fault that never recovered scores 1. Recovery is measured from the revert until the journeys are back inside the baseline band.
- overall weights: each service counts once per journey it sits on in the dependency map (at least 1); a service seen degrading more journeys than the map lists uses the observed count. Untested services are excluded and listed.
- rating: 80 and above good, 60 to 79 fair, below 60 poor.

## Methodology

- An independent synthetic prober exercises four user journeys (home, product, cart, checkout) through the front proxy every second. It is the ground truth and does not depend on the application's own telemetry.
- A baseline run fixes per-journey error rate and p95 latency and derives safety thresholds from them. A watchdog aborts any experiment that breaches a threshold or loses sight of the prober.
- Each experiment injects one fault (container crash, added network delay or packet loss), measures every journey during the fault window (up to the abort for aborted runs), reverts the fault, and measures how long the journeys take to return inside the baseline band.
- Impact, breadth, recovery and the penalty are computed deterministically from those measurements. The hypothesis verdict compares the journeys the planner predicted would degrade with those that actually did: CONFIRMED when equal, PARTIAL when they overlap, REFUTED when they do not.
- A hidden dependency is a journey that degraded by at least 0.3 although neither the hypothesis nor the dependency map expected it.
- The analyst agent ranks weaknesses and the fixer agent proposes fixes from the measured results only; their output is validated against known experiments, services and an allowlist of compose keys. When Groq is unavailable or its output is invalid, rule-based fallbacks produce the same sections. Fixes are proposals and are never applied.

## AI usage

| Agent | Path | Groq calls | Fallbacks | Cached | Prompt tokens | Completion tokens |
|---|---|---|---|---|---|---|
| planner | groq openai/gpt-oss-120b | 3 | 0 | 0 | 5192 | 1340 |
| safety | not run | 0 | 0 | 0 | 0 | 0 |
| analyst | groq openai/gpt-oss-120b | 1 | 0 | 0 | 3324 | 4403 |
| fixer | groq openai/gpt-oss-120b | 1 | 0 | 0 | 953 | 1268 |

Analyst weaknesses dropped by validation: 0. Fixes filled by the rule-based catalog: 0.
