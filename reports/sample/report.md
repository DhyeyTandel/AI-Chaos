# Faultline resilience report

| Run | Started | Ended | Rounds | Final tier | Stop reason |
|---|---|---|---|---|---|
| run-165931-61dc | 2026-10-10 16:59:31 UTC | 2026-10-10 17:01:48 UTC | 3/3 | 2 | completed 3 rounds |

**37.5 / 100**  
Overall resilience score (poor). Generated 2026-10-10 17:01:55 UTC.

## Executive summary

The shop's resilience is compromised by several critical weaknesses. A hidden coupling between astronomy-db and checkout causes checkout latency breaches even when not predicted, making checkout vulnerable to unrelated service slowdowns. The cart service itself is a single point of failure; its crash aborts all cart journeys. Front-end components (frontend, frontend-proxy, product-catalog) amplify tiny delays into system-wide outages, showing an overly broad blast radius. Additionally, the valkey-cart cache and flagd service expose hidden dependencies that can cascade failures to the cart. Add

## Ranked weaknesses and proposed fixes

Fixes are proposals only. Nothing was applied to the running system.

### 1. Hidden checkout dependency [CRITICAL]

Services: astronomy-db. Experiments: exp-143142-3011, exp-163723-5939, exp-141736-9801, exp-165933-0066. Source: llm.

Delays on astronomy-db repeatedly breach the checkout latency threshold (>800 ms) even though checkout was not in the predicted journey map, indicating an undocumented coupling that can degrade checkout performance across all user flows.

Evidence: exp-143142-3011, exp-163723-5939, exp-141736-9801, exp-165933-0066 all show checkout p95 >800 ms, impact 1.00, breadth 1.00, penalty 82-66.

Proposed fix (code, llm): Add timeout and retry to checkout calls to astronomy-db. Wrap all calls from checkout service to astronomy-db with a client library that sets a 200 ms timeout and retries up to 2 times with exponential backoff. Log fallback latency. This isolates checkout from transient DB delays.

Expected effect: Checkout latency stays under 800 ms even when astronomy-db experiences 100-300 ms delays.

### 2. Cart service single point of failure [HIGH]

Services: cart. Experiments: exp-142250-d200. Source: llm.

A crash of the cart service aborts all cart-related journeys, causing a full-impact outage (impact 1.00) despite a limited blast radius, revealing the cart as a critical single point of failure.

Evidence: exp-142250-d200 crash on cart, impact 1.00, breadth 0.25, recovery 6.01 s, penalty 60.

Proposed fix (config, llm): Run cart service with replica and restart. Update docker-compose to define two replicas of cart service and set restart: unless-stopped. Use deploy.resources limits to ensure each replica gets sufficient CPU/memory.

Compose override (not applied):

```
services:
  cart:
    restart: unless-stopped
    deploy:
      resources:
        limits:
          cpus: '0.5'
          memory: 512M
```

Expected effect: Cart crash affects only one replica; other continues serving, eliminating full-outage.

### 3. Broad blast radius from frontend delays [HIGH]

Services: frontend, frontend-proxy, product-catalog. Experiments: exp-141542-f9bb, exp-164224-dcfe, exp-170022-0d38. Source: llm.

Small latency injections (100 ms) on frontend-related services trigger watchdog breaches across all major journeys, showing that latency in these front-door components propagates system-wide.

Evidence: exp-141542-f9bb (frontend), exp-164224-dcfe (product-catalog), exp-170022-0d38 (frontend-proxy) each have impact 1.00, breadth 1.00 (or 0.75), penalty >=53.

Proposed fix (code, llm): Circuit breaker on frontend entry points. Introduce a circuit-breaker middleware in frontend and frontend-proxy that opens after 3 consecutive >100 ms responses, returning a cached fallback page. Include retry with short back-off for product-catalog calls.

Expected effect: Latency spikes in entry services no longer cascade, keeping overall journeys within thresholds.

### 4. Valkey-cart cache single point [MEDIUM]

Services: valkey-cart. Experiments: exp-104221-fd4f. Source: llm.

Crashing the in-memory cache degrades the cart journey to the maximum impact, indicating that loss of the cache cannot be tolerated without severe user-experience degradation.

Evidence: exp-104221-fd4f crash on valkey-cart, impact 1.00, breadth 0.25, recovery 6.01 s, penalty 60.

Proposed fix (config, llm): Add replica and fallback for valkey-cart. Deploy two instances of valkey-cart with restart: unless-stopped. Configure cart service to fall back to persistent DB if both caches miss or are unavailable.

Compose override (not applied):

```
services:
  valkey-cart:
    restart: unless-stopped
    deploy:
      resources:
        limits:
          cpus: '0.3'
          memory: 256M
```

Expected effect: Cache crash no longer degrades cart journey; cart continues using DB fallback.

### 5. Flagd hidden cart dependency [MEDIUM]

Services: flagd. Experiments: exp-141616-6842. Source: llm.

A crash of flagd, though refuted, still shows an undocumented dependency on the cart service, causing cart latency spikes and highlighting hidden coupling that could surface under real failures.

Evidence: exp-141616-6842 crash on flagd, hidden dependency on cart, impact 1.00, breadth 0.25, penalty 60.

Proposed fix (code, llm): Feature flag client with timeout and fallback. Wrap flagd client in cart service with a 150 ms timeout and default flag values. On timeout or error, use cached defaults to avoid latency spikes.

Expected effect: Flagd failure does not propagate latency to cart, removing hidden coupling impact.

## Experiments in this run

| Round | Tier | Fault | Target | AI reason | Hypothesis | Verdict | Degraded journeys | Impact | Breadth | Recovery | Penalty | Safety review | Safety agent | Outcome |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 1 | delay 50ms | astronomy-db | Highest det_score and stateful; close to known abort limit (300ms) so 50ms is safe yet probes hidden DB dependency across multiple journeys. | Injecting a 50 ms delay into astronomy-db will increase latency of home, product, and cart journeys, exposing any hidden dependency on th... | PARTIAL | cart, checkout, home, product | 0.70 | 1.00 | 5.0s | 66 | approved: delay 50ms < known abort threshold 100ms, tier1, no recent aborts | 2 checks: 1 continue, 1 abort, 0 ignored | ABORTED by safety_agent (safety agent: checkout p95 285ms > baseline 74ms, outside...) |
| 2 | 1 | delay 50ms | frontend-proxy | Delays a hub that all journeys pass through; shows single-point impact and tests latency tolerance. | If frontend-proxy gets added network latency (delay 50ms), callers (otel-collector) will show errors or higher latency and journeys (home... | PARTIAL | cart, checkout, home | 0.57 | 0.75 | 5.0s | 53 | approved: Tier1, 50ms delay completed before, no aborts at this intensity | 2 checks: 2 continue, 0 abort, 0 ignored | COMPLETED |
| 3 | 1 | delay 50ms | cart | Tier 1 light delay on cart, highest det_score and 0.5 journey impact; recent result survived, so fits survivable tier and probes a statef... | Injecting a 50 ms delay into the cart service will increase latency of the checkout journey and may cause degraded p95 for checkout while... | CONFIRMED | cart, checkout | 0.83 | 0.50 | 5.0s | 58 | approved: Tier1 delay 50ms, duration 20s within abort limits; no prior aborts at this intensity; recent 100ms fronten... | 2 checks: 2 continue, 0 abort, 0 ignored | COMPLETED |

Vetoed by the pre-flight safety review (not injected, not counted as aborts): round 2 crash:image-provider:20s (Crash on image-provider likely breaches frontend abort limits (100ms delay aborted), recent similar abort observed.)

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
| exp-164043-141e | delay 50ms | flagd | cart, checkout | 0.44 | 0.50 | 5.0s | 38 | COMPLETED |
| exp-164133-9202 | delay 50ms | frontend-proxy | cart, checkout, home | 0.57 | 0.75 | 5.0s | 53 | COMPLETED |
| exp-164224-dcfe | delay 300ms | product-catalog | cart, checkout, home, product | 1.00 | 1.00 | 5.0s | 82 | ABORTED |

## Learned limits

Lowest fault intensity per target and fault kind that the watchdog or safety agent aborted in the last 7 days, as known when this run started. The planner does not propose that intensity or a higher one.

| Target | Fault | Lowest aborted intensity | Aborted by | Evidence |
|---|---|---|---|---|
| astronomy-db | delay | 300 ms | watchdog | exp-143142-3011 |
| cart | crash | crash | watchdog | exp-142250-d200 |
| flagd | crash | crash | watchdog | exp-141616-6842 |
| frontend | delay | 100 ms | watchdog | exp-141542-f9bb |
| product-catalog | delay | 100 ms | watchdog | exp-141653-b9e0 |
| valkey-cart | crash | crash | watchdog | exp-104221-fd4f |

New limits learned in this run: delay 50ms on astronomy-db aborted by safety_agent (exp-165933-0066).

## Hidden dependencies

| Experiment | Scope | Fault | Target | Unexpected journeys | Worst degradation | Map |
|---|---|---|---|---|---|---|
| exp-141616-6842 | history | crash | flagd | cart | 1.00 | map shows no journey depending on it |
| exp-143142-3011 | history | delay 300ms | astronomy-db | checkout | 1.00 | map lists journeys cart, home, product |
| exp-163723-5939 | history | delay 300ms | astronomy-db | checkout | 1.00 | map lists journeys cart, home, product |
| exp-165933-0066 | run | delay 50ms | astronomy-db | checkout | 0.70 | map lists journeys cart, home, product |
| exp-141736-9801 | history | delay 100ms | astronomy-db | checkout | 0.67 | map lists journeys cart, home, product |
| exp-142108-4440 | history | delay 50ms | astronomy-db | checkout | 0.44 | map lists journeys cart, home, product |
| exp-164043-141e | history | delay 50ms | flagd | checkout | 0.44 | map shows no journey depending on it |

## Per-service scores

| Service | Score | Worst experiment | Fault | Experiments | Weight |
|---|---|---|---|---|---|
| astronomy-db | 18.3 | exp-143142-3011 | delay 300ms | 5 | 4 |
| frontend | 18.3 | exp-141542-f9bb | delay 100ms | 1 | 4 |
| product-catalog | 18.3 | exp-164224-dcfe | delay 300ms | 2 | 4 |
| cart | 40.5 | exp-142250-d200 | crash | 2 | 2 |
| flagd | 40.5 | exp-141616-6842 | crash | 2 | 2 |
| valkey-cart | 40.5 | exp-104221-fd4f | crash | 2 | 2 |
| frontend-proxy | 47.3 | exp-170022-0d38 | delay 50ms | 3 | 4 |
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
- A baseline run fixes per-journey error rate and p95 latency and derives safety thresholds from them. A watchdog aborts any experiment that breaches a threshold or loses sight of the prober. Before injection, the safety agent reviews the plan against the baseline limits, recent outcomes and the learned limits, and may approve it, reduce its intensity or veto it; it can never raise an intensity. The planner remembers, across runs, the lowest intensity per target and fault kind that the watchdog or safety agent aborted, and does not propose that intensity or a higher one again within the lookback window. An advisory safety agent re-checks the live measurements every few seconds and may abort earlier; it can never cancel a watchdog abort, extend a fault or change a threshold.
- Each experiment injects one fault (container crash, added network delay or packet loss), measures every journey during the fault window (up to the abort for aborted runs), reverts the fault, and measures how long the journeys take to return inside the baseline band.
- Impact, breadth, recovery and the penalty are computed deterministically from those measurements. The hypothesis verdict compares the journeys the planner predicted would degrade with those that actually did: CONFIRMED when equal, PARTIAL when they overlap, REFUTED when they do not.
- A hidden dependency is a journey that degraded by at least 0.3 although neither the hypothesis nor the dependency map expected it.
- The analyst agent ranks weaknesses and the fixer agent proposes fixes from the measured results only; their output is validated against known experiments, services and an allowlist of compose keys. When Groq is unavailable or its output is invalid, rule-based fallbacks produce the same sections. Fixes are proposals and are never applied.

## AI usage

| Agent | Path | Groq calls | Fallbacks | Cached | Prompt tokens | Completion tokens |
|---|---|---|---|---|---|---|
| planner | groq openai/gpt-oss-120b | 3 | 0 | 0 | 5613 | 1787 |
| safety | groq openai/gpt-oss-20b | 10 | 0 | 0 | 6570 | 793 |
| analyst | groq openai/gpt-oss-120b | 1 | 0 | 0 | 1560 | 1744 |
| fixer | groq openai/gpt-oss-120b | 1 | 0 | 0 | 901 | 1292 |

Safety agent: 6 checks across 3 experiments (continue 5, abort 1, ignored 0). Aborts by watchdog: 0, by safety agent: 1. Ignored checks (timeout, rate limit, invalid output or budget) leave the watchdog verdict standing.

Pre-flight review: 3 reviews of executed experiments (approve 3, reduce 0, fallback approvals 0) and 1 vetoes that skipped a candidate. A veto counts as a planner attempt, not as an abort. A failed review approves, and the validator and watchdog still apply.

Analyst weaknesses dropped by validation: 0. Fixes filled by the rule-based catalog: 0.
