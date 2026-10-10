# Faultline resilience report

| Run | Started | Ended | Rounds | Final tier | Stop reason |
|---|---|---|---|---|---|
| run-184204-9283 | 2026-10-10 18:42:04 UTC | 2026-10-10 18:43:22 UTC | 2/2 | 2 | completed 2 rounds |

**39.6 / 100**  
Overall resilience score (poor). Generated 2026-10-10 18:43:30 UTC.

## Executive summary

The shop's resilience is compromised by several critical weaknesses. A hidden dependency links astronomy-db to checkout, causing checkout failures whenever astronomy-db is delayed. Core caches and services (valkey-cart, flagd, cart) act as single points of failure, each crashing and halting cart operations with no fallback. Additionally, product-catalog delays have a wide blast radius, degrading all primary journeys. Addressing these hidden couplings, adding redundancy, and isolating blast zones are top priorities to improve the overall score.

## Ranked weaknesses and proposed fixes

Fixes are proposals only. Nothing was applied to the running system.

### 1. Hidden checkout dependency via astronomy-db [CRITICAL]

Services: astronomy-db. Experiments: exp-143142-3011, exp-163723-5939, exp-141736-9801, exp-165933-0066. Source: llm.

Delays on astronomy-db unexpectedly degrade checkout journeys, revealing a hidden coupling not captured in the service map. This can cause checkout latency spikes and aborts when astronomy-db is slow, threatening revenue flow.

Evidence: exp-143142-3011, exp-163723-5939, exp-141736-9801, exp-165933-0066 all show checkout p95 >800ms and degraded checkout despite not being predicted.

Proposed fix (code, llm): Add circuit breaker & cache for astronomy-db calls. In the checkout service, wrap all calls to astronomy-db with a circuit-breaker library (e.g., resilience4j). Configure a 100 ms timeout, open the circuit after 3 failures, and provide a fallback that returns cached astronomy data populated at startup and refreshed asynchronously. Log circuit state changes for observability.

Expected effect: Checkout latency stays below p95 threshold even when astronomy-db is delayed; aborts are avoided and revenue flow is protected.

### 2. Single point of failure: valkey-cart [HIGH]

Services: valkey-cart. Experiments: exp-104221-fd4f. Source: llm.

A crash of valkey-cart aborts all cart journeys with impact 1.00 but affects only the cart (breadth 0.25), showing no fallback or redundancy for the core cart cache.

Evidence: exp-104221-fd4f crash on valkey-cart caused cart p95 3007ms, degraded cart only, breadth 0.25.

Proposed fix (config, llm): Enable restart policy for valkey-cart and add in-process fallback. Edit docker-compose.yml to add restart: unless-stopped for valkey-cart. In the cart service, add a lightweight in-memory cache that is used when Redis is unavailable, populated from Redis on successful connections. This provides continuity until Redis restarts.

Compose override (not applied):

```
services:
  valkey-cart:
    restart: unless-stopped
```

Expected effect: A valkey-cart crash triggers automatic container restart; cart requests fall back to in-process cache, preventing total cart outage.

### 3. Single point of failure: flagd with hidden cart dependency [HIGH]

Services: flagd. Experiments: exp-141616-6842. Source: llm.

Flagd crash aborts cart journeys, indicating an undocumented dependency of the cart on flagd. No other journeys are affected, exposing a fragile link in the checkout path.

Evidence: exp-141616-6842 crash on flagd caused cart p95 3051ms, degraded cart only, breadth 0.25, hidden dependency on cart.

Proposed fix (code, llm): Circuit-break flagd calls in cart service. Introduce a circuit-breaker around every flagd request in the cart service (e.g., using opossum). Set a 50 ms timeout and a fallback that returns default flag values. Record metrics for circuit state to aid debugging.

Expected effect: Cart journeys continue with sensible defaults when flagd crashes, eliminating the hidden dependency impact.

### 4. Single point of failure: cart service [HIGH]

Services: cart. Experiments: exp-142250-d200. Source: llm.

The cart service crash directly aborts cart journeys with impact 1.00 and breadth 0.25, confirming the cart as a critical single point without automatic failover.

Evidence: exp-142250-d200 crash on cart caused cart p95 3007ms, degraded cart only, breadth 0.25.

Proposed fix (config, llm): Add restart policy for cart service. Update docker-compose.yml to set restart: unless-stopped for the cart service. Optionally define modest CPU/memory limits under deploy.resources to keep the container lightweight. This ensures the cart container is automatically relaunched after a crash.

Compose override (not applied):

```
services:
  cart:
    restart: unless-stopped
```

Expected effect: Cart service recovers automatically after a crash, reducing downtime and keeping cart journeys available.

### 5. Wide blast radius: product-catalog delays [HIGH]

Services: product-catalog. Experiments: exp-164224-dcfe. Source: llm.

A 300 ms delay on product-catalog triggers watchdog breaches across cart, checkout, home, and product journeys, degrading all major user flows and showing a large blast radius.

Evidence: exp-164224-dcfe delay 300ms on product-catalog caused product p95 1226ms, cart p95 1250ms, degraded cart,checkout,home,product, breadth 1.00.

Proposed fix (code, llm): Timeout, retries and caching for product-catalog calls. Wrap product-catalog client calls in all dependent services with a wrapper that enforces a 200 ms timeout, performs up to 2 retries with exponential backoff, and caches successful responses for 5 minutes. Use a library like axios-retry or similar. Log retry attempts and cache hits.

Expected effect: Downstream services tolerate product-catalog latency spikes, preventing watchdog breaches and limiting blast radius across all journeys.

## Experiments in this run

| Round | Tier | Fault | Target | AI reason | Hypothesis | Verdict | Degraded journeys | Impact | Breadth | Recovery | Penalty | Safety review | Safety agent | Outcome |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 1 | crash | image-provider | Tier 1 crash of image-provider is untested, high fan-in and severity 1, exposing hidden dependencies (frontend-proxy, load-generator) wit... | Crashing image-provider for 20 s will cause frontend-proxy and load-generator requests to fail, revealing hidden dependencies and confirm... | CONFIRMED | none | 0.00 | 0.00 | 5.0s | 2 | reduced duration 20 -> 10 s: Crash may exceed 20% err; recent abort on frontend loss 10% suggests caution. Reduce dur... | 1 checks: 1 continue, 0 abort, 0 ignored | COMPLETED |
| 2 | 1 | delay 50ms | flagd | Tier 1 light delay on flagd sits near the known crash limit for flagd and has the highest fan-in, exposing hidden cross-service dependenc... | Injecting a 50 ms delay into flagd will surface hidden dependencies across multiple services, causing measurable latency spikes in downst... | CONFIRMED | cart, checkout | 0.44 | 0.50 | 5.0s | 38 | approved: delay 50ms on flagd, tier1, predicted p95 ~120ms < abort 800ms, similar to cart delay 50ms completed | 2 checks: 2 continue, 0 abort, 0 ignored | COMPLETED |

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
| exp-165933-0066 | delay 50ms | astronomy-db | cart, checkout, home, product | 0.70 | 1.00 | 5.0s | 66 | ABORTED |
| exp-170022-0d38 | delay 50ms | frontend-proxy | cart, checkout, home | 0.57 | 0.75 | 5.0s | 53 | COMPLETED |
| exp-170114-0a67 | delay 50ms | cart | cart, checkout | 0.83 | 0.50 | 5.0s | 58 | COMPLETED |
| exp-173527-9957 | delay 50ms | valkey-cart | cart, checkout | 0.63 | 0.50 | 5.0s | 48 | COMPLETED |
| exp-173618-19fc | delay 50ms | frontend-proxy | cart, checkout, home | 0.57 | 0.75 | 5.0s | 52 | COMPLETED |
| exp-173710-08a3 | loss 10% | frontend | cart, checkout, home | 0.96 | 0.75 | 5.0s | 72 | ABORTED |
| exp-183210-dd26 | delay 50ms | quote | checkout | 0.23 | 0.25 | not measured | 19 | ABORTED |

## Learned limits

Lowest fault intensity per target and fault kind that the watchdog or safety agent aborted in the last 7 days, as known when this run started. The planner does not propose that intensity or a higher one.

| Target | Fault | Lowest aborted intensity | Aborted by | Evidence |
|---|---|---|---|---|
| astronomy-db | delay | 50 ms | safety_agent | exp-165933-0066 |
| cart | crash | crash | watchdog | exp-142250-d200 |
| flagd | crash | crash | watchdog | exp-141616-6842 |
| frontend | delay | 100 ms | watchdog | exp-141542-f9bb |
| frontend | loss | 10 % | watchdog | exp-173710-08a3 |
| product-catalog | delay | 100 ms | watchdog | exp-141653-b9e0 |
| valkey-cart | crash | crash | watchdog | exp-104221-fd4f |

Candidates excluded during this run:

| Candidate | Reason | Evidence |
|---|---|---|
| delay:astronomy-db:50ms | delay on astronomy-db aborted at 50 ms in exp-165933-0066; 50 ms excluded | exp-165933-0066 |

## Hidden dependencies

| Experiment | Scope | Fault | Target | Unexpected journeys | Worst degradation | Map |
|---|---|---|---|---|---|---|
| exp-141616-6842 | history | crash | flagd | cart | 1.00 | map shows no journey depending on it |
| exp-143142-3011 | history | delay 300ms | astronomy-db | checkout | 1.00 | map lists journeys cart, home, product |
| exp-163723-5939 | history | delay 300ms | astronomy-db | checkout | 1.00 | map lists journeys cart, home, product |
| exp-165933-0066 | history | delay 50ms | astronomy-db | checkout | 0.70 | map lists journeys cart, home, product |
| exp-141736-9801 | history | delay 100ms | astronomy-db | checkout | 0.67 | map lists journeys cart, home, product |
| exp-142108-4440 | history | delay 50ms | astronomy-db | checkout | 0.44 | map lists journeys cart, home, product |
| exp-164043-141e | history | delay 50ms | flagd | checkout | 0.44 | map shows no journey depending on it |

## Per-service scores

| Service | Score | Worst experiment | Fault | Experiments | Weight |
|---|---|---|---|---|---|
| astronomy-db | 18.3 | exp-143142-3011 | delay 300ms | 5 | 4 |
| frontend | 18.3 | exp-141542-f9bb | delay 100ms | 2 | 4 |
| product-catalog | 18.3 | exp-164224-dcfe | delay 300ms | 2 | 4 |
| cart | 40.5 | exp-142250-d200 | crash | 2 | 2 |
| flagd | 40.5 | exp-141616-6842 | crash | 3 | 2 |
| valkey-cart | 40.5 | exp-104221-fd4f | crash | 3 | 2 |
| frontend-proxy | 47.3 | exp-170022-0d38 | delay 50ms | 4 | 4 |
| currency | 60.2 | exp-100938-5e55 | loss 10% | 11 | 4 |
| quote | 60.2 | exp-140231-db3a | delay 100ms | 3 | 1 |
| image-provider | 98.3 | exp-184207-ebed | crash | 1 | 1 |
| recommendation | 98.3 | exp-100645-5f9e | loss 10% | 1 | 1 |

Untested services (excluded from the overall score): accounting, ad, checkout, email, fraud-detection, kafka, payment, shipping

## Score formula

```
penalty = 100 x (0.5 x impact + 0.3 x breadth + 0.2 x recovery)
service score = 100 - worst penalty observed on that service
overall = mean of service scores weighted by journey dependency
```

- impact: worst journey degradation in the fault window. Per journey, max(error part, latency part). Error part = (error rate - baseline error rate) / 0.5, clamped to 0..1. Latency part = ln(p95 / max(baseline p95, 100 ms)) / ln(10), clamped to 0..1.
- breadth: affected journeys / all journeys. A journey is affected when its degradation is at least 0.2 or it breaches the baseline threshold.
- recovery: min(1, recovery seconds / 60); a fault that never recovered scores 1, and an experiment stopped by the operator before recovery was measured contributes 0. Recovery is measured from the revert until the journeys are back inside the baseline band.
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
| planner | groq openai/gpt-oss-120b | 2 | 0 | 0 | 3839 | 1098 |
| safety | groq openai/gpt-oss-20b | 5 | 0 | 0 | 3331 | 567 |
| analyst | groq openai/gpt-oss-120b | 1 | 0 | 0 | 1577 | 1949 |
| fixer | groq openai/gpt-oss-120b | 1 | 0 | 0 | 852 | 1332 |

Safety agent: 3 checks across 2 experiments (continue 3, abort 0, ignored 0). Aborts by watchdog: 0, by safety agent: 0. Ignored checks (timeout, rate limit, invalid output or budget) leave the watchdog verdict standing.

Pre-flight review: 2 reviews of executed experiments (approve 1, reduce 1, fallback approvals 0) and 0 vetoes that skipped a candidate. A veto counts as a planner attempt, not as an abort. A failed review approves, and the validator and watchdog still apply.

Analyst weaknesses dropped by validation: 0. Fixes filled by the rule-based catalog: 0.

Recovery was not measured for exp-183210-dd26 (stopped by the operator before recovery could be measured). Their recovery component is excluded from the penalty (counted as 0), not scored as never recovered.
