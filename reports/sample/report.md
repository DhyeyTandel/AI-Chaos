# Faultline resilience report

| Run | Started | Ended | Rounds | Final tier | Stop reason |
|---|---|---|---|---|---|
| run-164041-4da0 | 2026-10-10 16:40:41 UTC | 2026-10-10 16:42:42 UTC | 3/3 | 2 | completed 3 rounds |

**37.6 / 100**  
Overall resilience score (poor). Generated 2026-10-10 16:42:49 UTC.

## Executive summary

The shop's resilience is compromised by a few critical weaknesses. Latency on core services (product-catalog, frontend, astronomy-db) instantly degrades all user journeys, indicating a wide blast radius. Hidden dependencies - astronomy-db affecting checkout and flagd affecting cart - are not captured in the current dependency map, exposing unexpected failure paths. Additionally, valkey-cart and cart themselves act as single points of failure, each causing full cart-journey loss when they crash. Addressing these issues - tightening latency budgets, mapping hidden couplings, and adding redundanc

## Ranked weaknesses and proposed fixes

Fixes are proposals only. Nothing was applied to the running system.

### 1. Wide blast radius from latency on core services [CRITICAL]

Services: product-catalog, frontend, astronomy-db. Experiments: exp-164224-dcfe, exp-141542-f9bb, exp-143142-3011. Source: llm.

Injecting 300ms latency on product-catalog, frontend or astronomy-db aborts the experiment and triggers watchdog breaches, causing all four key journeys (cart, checkout, home, product) to degrade. The impact is 1.00 with full breadth, showing a single latency fault can cascade across the entire shop.

Evidence: exp-164224-dcfe (product-catalog) impact 1.00 breadth 1.00; exp-141542-f9bb (frontend) impact 1.00 breadth 1.00; exp-143142-3011 (astronomy-db) impact 1.00 breadth 1.00; all aborted due to p95 >800ms

Proposed fix (architecture, llm): Add circuit breaker, retries and replica scaling. Introduce a client-side circuit breaker (e.g., resilience4j) around calls from frontend to product-catalog and astronomy-db with a 500 ms timeout and 3 retries. Deploy each core service with 2 replicas and set restart: unless-stopped so containers auto-recover on crash.

Compose override (not applied):

```
services:
  product-catalog:
    restart: unless-stopped
  astronomy-db:
    restart: unless-stopped
  frontend:
    restart: unless-stopped
```

Expected effect: Latency faults no longer cascade; p95 stays below 800 ms and journeys complete even when one instance is delayed.

### 2. Hidden dependency on checkout via astronomy-db [HIGH]

Services: astronomy-db. Experiments: exp-143142-3011. Source: llm.

Delays on astronomy-db cause checkout journey latency breaches even though the dependency map did not predict checkout involvement. This hidden coupling expands failure impact beyond expected paths.

Evidence: exp-143142-3011 delay 300ms on astronomy-db degraded checkout (p95 1244ms >800ms); verdict PARTIAL; note "HIDDEN dependency on checkout"

Proposed fix (code, llm): Add retry, timeout and circuit breaker for checkout-astronomy-db calls. Wrap checkout service HTTP client calls to astronomy-db with a 400 ms timeout, 2 retries and a circuit breaker that opens after 5 consecutive failures. On open, return cached product data or a graceful error message. Log each fallback event.

Expected effect: Checkout journey remains within SLA despite 300 ms delays in astronomy-db, eliminating hidden coupling impact.

### 3. Hidden dependency on cart via flagd [HIGH]

Services: flagd. Experiments: exp-141616-6842. Source: llm.

A crash of flagd aborts the experiment and degrades the cart journey, despite no predicted journeys. This reveals an undocumented reliance of cart on flagd, a risk for unanticipated outages.

Evidence: exp-141616-6842 crash on flagd degraded cart (p95 3051ms >800ms); verdict REFUTED; note "HIDDEN dependency on cart"

Proposed fix (code, llm): Circuit breaker and fallback for flagd in cart. Instrument cart service flag-evaluation with a circuit breaker (200 ms timeout, 1 retry). When the breaker opens, use locally stored default flags. Deploy a second flagd replica and add restart: unless-stopped to ensure availability.

Compose override (not applied):

```
services:
  flagd:
    restart: unless-stopped
```

Expected effect: Cart journey no longer fails when flagd crashes; fallback flags keep cart functional.

### 4. Single point of failure in valkey-cart [HIGH]

Services: valkey-cart. Experiments: exp-104221-fd4f. Source: llm.

Crashing valkey-cart aborts the experiment and fully impacts the cart journey, with a breadth of 0.25. The service is a sole source for cart state, making its failure a critical SPOF.

Evidence: exp-104221-fd4f crash on valkey-cart impact 1.00 breadth 0.25; degraded cart; verdict PARTIAL

Proposed fix (config, llm): Replicate valkey-cart with failover. Run two valkey-cart containers behind DNS round-robin. Configure the cart client to use Redis Sentinel-style failover with a 300 ms reconnect timeout. Add restart: unless-stopped to both instances to auto-recover.

Compose override (not applied):

```
services:
  valkey-cart:
    restart: unless-stopped
```

Expected effect: Valkey-cart crash no longer aborts experiments; cart state remains available via failover.

### 5. Single point of failure in cart service [MEDIUM]

Services: cart. Experiments: exp-142250-d200. Source: llm.

A crash of the cart service aborts the experiment and fully degrades the cart journey, confirming cart as a SPOF for its own functionality and for downstream checkout.

Evidence: exp-142250-d200 crash on cart impact 1.00 breadth 0.25; degraded cart; verdict PARTIAL

Proposed fix (config, llm): Scale cart service and add retry logic. Deploy two cart service replicas behind the existing load balancer. Add restart: unless-stopped. In internal cart calls, add a 2-retry policy with a 250 ms timeout to handle transient failures.

Compose override (not applied):

```
services:
  cart:
    restart: unless-stopped
```

Expected effect: Cart service is no longer a single point of failure; crashes are absorbed by the replica and retries keep journeys alive.

## Experiments in this run

| Round | Tier | Fault | Target | AI reason | Hypothesis | Verdict | Degraded journeys | Impact | Breadth | Recovery | Penalty | Safety agent | Outcome |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 1 | delay 50ms | flagd | Tier 1 light delay on flagd is fully untested and has the highest fan-in, exposing hidden cross-service dependencies without risking outage. | Injecting a 50 ms delay into flagd will surface latency propagation through all its callers (ad, cart, checkout, email, frontend, fraud-d... | CONFIRMED | cart, checkout | 0.44 | 0.50 | 5.0s | 38 | 2 checks: 2 continue, 0 abort, 0 ignored | COMPLETED |
| 2 | 1 | delay 50ms | frontend-proxy | Tier-1 light delay on frontend-proxy touches all journeys, exposing hidden upstream dependencies and single-point-of-failure risk after t... | Delaying frontend-proxy by 50 ms will increase latency across all journeys, exposing any hidden coupling or bottlenecks in the proxy layer. | PARTIAL | cart, checkout, home | 0.57 | 0.75 | 5.0s | 53 | 2 checks: 2 continue, 0 abort, 0 ignored | COMPLETED |
| 3 | 2 | delay 300ms | product-catalog | Tier 2 light delay on a core catalog service; previous tier-2 delays (flagd,frontend-proxy) survived, so this tests a new off-journey but... | Delaying product-catalog by 300 ms will increase latency for home, product, cart, and checkout journeys, exposing any downstream timeout ... | CONFIRMED | cart, checkout, home, product | 1.00 | 1.00 | 5.0s | 82 | - | ABORTED by watchdog (watchdog breach: product p95 1226ms > 800ms; cart p95 125...) |

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
| exp-142959-df17 | delay 50ms | valkey-cart | cart, checkout | 0.63 | 0.50 | 5.0s | 48 | COMPLETED |
| exp-143050-96fc | delay 50ms | currency | checkout | 0.24 | 0.25 | 5.0s | 21 | COMPLETED |
| exp-143142-3011 | delay 300ms | astronomy-db | cart, checkout, home, product | 1.00 | 1.00 | 5.0s | 82 | ABORTED |
| exp-163536-419a | delay 100ms | quote | checkout | 0.56 | 0.25 | 5.0s | 37 | ABORTED |
| exp-163642-ac26 | loss 10% | currency | checkout | 0.50 | 0.25 | 5.0s | 34 | COMPLETED |
| exp-163723-5939 | delay 300ms | astronomy-db | cart, checkout, home, product | 1.00 | 1.00 | 5.0s | 82 | ABORTED |

## Hidden dependencies

| Experiment | Scope | Fault | Target | Unexpected journeys | Worst degradation | Map |
|---|---|---|---|---|---|---|
| exp-141616-6842 | history | crash | flagd | cart | 1.00 | map shows no journey depending on it |
| exp-143142-3011 | history | delay 300ms | astronomy-db | checkout | 1.00 | map lists journeys cart, home, product |
| exp-163723-5939 | history | delay 300ms | astronomy-db | checkout | 1.00 | map lists journeys cart, home, product |
| exp-141736-9801 | history | delay 100ms | astronomy-db | checkout | 0.67 | map lists journeys cart, home, product |
| exp-142108-4440 | history | delay 50ms | astronomy-db | checkout | 0.44 | map lists journeys cart, home, product |

## Per-service scores

| Service | Score | Worst experiment | Fault | Experiments | Weight |
|---|---|---|---|---|---|
| astronomy-db | 18.3 | exp-143142-3011 | delay 300ms | 4 | 4 |
| frontend | 18.3 | exp-141542-f9bb | delay 100ms | 1 | 4 |
| product-catalog | 18.3 | exp-164224-dcfe | delay 300ms | 2 | 4 |
| cart | 40.5 | exp-142250-d200 | crash | 1 | 2 |
| flagd | 40.5 | exp-141616-6842 | crash | 2 | 2 |
| valkey-cart | 40.5 | exp-104221-fd4f | crash | 2 | 2 |
| frontend-proxy | 47.4 | exp-164133-9202 | delay 50ms | 2 | 4 |
| currency | 60.2 | exp-100938-5e55 | loss 10% | 11 | 4 |
| quote | 60.2 | exp-140231-db3a | delay 100ms | 2 | 1 |
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
- A baseline run fixes per-journey error rate and p95 latency and derives safety thresholds from them. A watchdog aborts any experiment that breaches a threshold or loses sight of the prober. An advisory safety agent re-checks the live measurements every few seconds and may abort earlier; it can never cancel a watchdog abort, extend a fault or change a threshold.
- Each experiment injects one fault (container crash, added network delay or packet loss), measures every journey during the fault window (up to the abort for aborted runs), reverts the fault, and measures how long the journeys take to return inside the baseline band.
- Impact, breadth, recovery and the penalty are computed deterministically from those measurements. The hypothesis verdict compares the journeys the planner predicted would degrade with those that actually did: CONFIRMED when equal, PARTIAL when they overlap, REFUTED when they do not.
- A hidden dependency is a journey that degraded by at least 0.3 although neither the hypothesis nor the dependency map expected it.
- The analyst agent ranks weaknesses and the fixer agent proposes fixes from the measured results only; their output is validated against known experiments, services and an allowlist of compose keys. When Groq is unavailable or its output is invalid, rule-based fallbacks produce the same sections. Fixes are proposals and are never applied.

## AI usage

| Agent | Path | Groq calls | Fallbacks | Cached | Prompt tokens | Completion tokens |
|---|---|---|---|---|---|---|
| planner | groq openai/gpt-oss-120b | 3 | 0 | 0 | 5179 | 1514 |
| safety | groq openai/gpt-oss-20b | 4 | 0 | 0 | 2534 | 290 |
| analyst | groq openai/gpt-oss-120b | 1 | 0 | 0 | 1530 | 1500 |
| fixer | groq openai/gpt-oss-120b | 1 | 0 | 0 | 906 | 1671 |

Safety agent: 4 checks across 2 experiments (continue 4, abort 0, ignored 0). Aborts by watchdog: 1, by safety agent: 0. Ignored checks (timeout, rate limit, invalid output or budget) leave the watchdog verdict standing.

Analyst weaknesses dropped by validation: 0. Fixes filled by the rule-based catalog: 0.
