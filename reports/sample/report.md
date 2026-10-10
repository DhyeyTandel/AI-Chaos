# Faultline resilience report

| Run | Started | Ended | Rounds | Final tier | Stop reason |
|---|---|---|---|---|---|
| run-201605-9bf7 | 2026-10-10 20:16:05 UTC | 2026-10-10 20:18:49 UTC | 4/4 | 2 | completed 4 rounds |

**44.2 / 100**  
Overall resilience score (poor). Generated 2026-10-10 20:18:58 UTC.

## Executive summary

The shop's microservice architecture shows a critical weakness: small delays in several services instantly break all major user journeys, indicating a wide blast radius. The cart service and its valkey-cart cache act as single points of failure, collapsing the cart journey when they crash. Hidden coupling between astronomy-db and checkout further exposes undocumented dependencies. Finally, three core services have extremely low resilience scores, making them fragile under load. Addressing these issues - adding redundancy, isolating dependencies, and improving the weakest services - will raise 

## Ranked weaknesses and proposed fixes

Fixes are proposals. Only a config fix that maps to a restart policy or a raised memory or CPU limit on one allowlisted service can be applied, by `faultline fix` after confirmation, and it is then verified by re-running the experiment (see Fix verification). Healthcheck, replica, code and architecture changes are propose-only. Nothing is applied by generating this report.

### 1. Wide blast radius across core journeys [CRITICAL]

Services: astronomy-db, product-catalog, frontend, frontend-proxy, valkey-cart. Experiments: exp-141542-f9bb, exp-143142-3011, exp-164224-dcfe, exp-201645-55d7. Source: llm.

A small latency injection on any of these services aborts the whole set of user journeys (cart, checkout, home, product). The map predicts the same four journeys, showing that failures propagate far beyond the affected component.

Evidence: exp-141542-f9bb, exp-143142-3011, exp-164224-dcfe, exp-201645-55d7 all have impact 1.00, breadth 1.00, degrading cart,checkout,home,product.

Proposed fix (code, llm): Add circuit breaker and timeout in frontend-proxy. Add a circuit-breaker library (e.g., opossum) to the frontend-proxy. Wrap each outbound HTTP call to astronomy-db, product-catalog and valkey-cart with a breaker configured to open after 3 consecutive >80 ms latencies, a 30 s cool-down, and a fallback that returns a cached response or a graceful error page. Also set a per-request timeout of 200 ms so delayed calls are aborted before they cascade.

Propose-only (code change: propose-only).

Expected effect: Latency spikes no longer abort all journeys; calls that exceed the timeout are short-circuited, limiting impact to the affected endpoint.

### 2. Cart service is a single point of failure [HIGH]

Services: cart. Experiments: exp-142250-d200. Source: llm.

When the cart service crashes the cart journey fails completely while other journeys remain unaffected, indicating no redundancy for the core cart functionality.

Evidence: exp-142250-d200 crash on cart aborts cart journey, impact 1.00, breadth 0.25, only cart degraded.

Proposed fix (config, llm): Restart policy and healthcheck for cart service. Add a restart policy and healthcheck to the cart service and limit its memory to 512 MiB. The healthcheck polls /health every 15 s; if it fails 3 times Docker restarts the container. This ensures the cart service recovers automatically after a crash.

Auto-applicable with `faultline fix` (cart: restart unless-stopped, memory 512 MB); propose-only part: healthcheck.

Compose override:

```
services:
  cart:
    restart: unless-stopped
    healthcheck:
      test:
      - CMD
      - curl
      - -f
      - http://localhost:8080/health
      interval: 15s
      timeout: 5s
      retries: 3
    deploy:
      resources:
        limits:
          memory: 512M
```

Expected effect: Cart container is automatically restarted on crash and stays within memory limits, reducing downtime to seconds and keeping cart journey available.

### 3. Valkey-cart cache is a single point of failure [HIGH]

Services: valkey-cart. Experiments: exp-104221-fd4f. Source: llm.

A crash of the valkey-cart cache stops the cart journey, showing the cart flow depends entirely on this cache with no fallback.

Evidence: exp-104221-fd4f crash on valkey-cart aborts cart, impact 1.00, breadth 0.25, only cart degraded.

Proposed fix (architecture, llm): Add Valkey replica with Sentinel failover. Deploy a second valkey-cart instance and run a Valkey Sentinel container to provide automatic failover. Update the cart client configuration to point to the sentinel address so reads/writes are redirected to the surviving node if one crashes. This removes the single point of failure.

Propose-only (architecture change: propose-only).

Expected effect: If one valkey-cart node crashes, Sentinel promotes the replica and the cart service continues without interruption.

### 4. Hidden dependencies cause unexpected checkout degradation [MEDIUM]

Services: astronomy-db. Experiments: exp-165933-0066. Source: llm.

A tiny 50 ms delay on astronomy-db triggers a safety-agent alert for checkout latency, even though checkout is outside the predicted blast radius, revealing undocumented coupling.

Evidence: exp-165933-0066 delay 50 ms on astronomy-db shows checkout p95 285 ms > baseline 74 ms, yet checkout degraded.

Proposed fix (code, llm): Timeout and cache fallback for astronomy-db calls in checkout. In the checkout service, wrap calls to astronomy-db with a timeout of 100 ms and a circuit-breaker that falls back to a read-through cache populated on successful calls. If the DB is delayed, the request returns cached data and the checkout latency stays within SLA.

Propose-only (code change: propose-only).

Expected effect: Checkout latency stays below SLA even when astronomy-db is delayed, preventing hidden-dependency cascades.

## Fix verification

Each applied fix was made through the Docker API on the running container, the same experiment was re-run with the same parameters, and the mean penalty before and after was compared. The before data is unchanged; after-fix experiments are listed separately and are not counted in the scores above. A fix is kept only when the verdict is IMPROVED (default); otherwise the original values are restored.

| Fix | Weakness | Service and change | Before penalty | After penalty | Verdict | State |
|---|---|---|---|---|---|---|
| #1 | 1 | frontend: memory 512 MB | 1.7 | 8.2 | WORSE | REVERTED |
| #2 | 1 | quote: memory 128 MB | 21.1 | 24.1 | NO CHANGE | REVERTED |

Fix #1 journey metrics during the fault window, before and after:

| Experiments (before -> after) | Journey | p95 ms before | p95 ms after | err % before | err % after |
|---|---|---|---|---|---|
| exp-194224-c92a -> exp-195150-76cf | home | 51 | 76 | 0.0 | 0.0 |
| exp-194224-c92a -> exp-195150-76cf | product | 82 | 128 | 0.0 | 0.0 |
| exp-194224-c92a -> exp-195150-76cf | cart | 64 | 120 | 0.0 | 0.0 |
| exp-194224-c92a -> exp-195150-76cf | checkout | 47 | 135 | 0.0 | 0.0 |

Fix #1 after-fix experiments:

| Experiment | Fault | Target | Degraded journeys | Impact | Breadth | Recovery | Penalty | Outcome |
|---|---|---|---|---|---|---|---|---|
| exp-195150-76cf | spike 100 users | traffic | none | 0.13 | 0.00 | 5.0s | 8 | COMPLETED |

Fix #2 journey metrics during the fault window, before and after:

| Experiments (before -> after) | Journey | p95 ms before | p95 ms after | err % before | err % after |
|---|---|---|---|---|---|
| exp-195618-be08 -> exp-195712-7c7a | home | 50 | 59 | 0.0 | 0.0 |
| exp-195618-be08 -> exp-195712-7c7a | product | 106 | 110 | 0.0 | 0.0 |
| exp-195618-be08 -> exp-195712-7c7a | cart | 69 | 93 | 0.0 | 0.0 |
| exp-195618-be08 -> exp-195712-7c7a | checkout | 173 | 199 | 0.0 | 0.0 |

Fix #2 after-fix experiments:

| Experiment | Fault | Target | Degraded journeys | Impact | Breadth | Recovery | Penalty | Outcome |
|---|---|---|---|---|---|---|---|---|
| exp-195712-7c7a | delay 50ms | quote | checkout | 0.30 | 0.25 | 5.0s | 24 | COMPLETED |

## Experiments in this run

| Round | Tier | Fault | Target | AI reason | Hypothesis | Verdict | Degraded journeys | Impact | Breadth | Recovery | Penalty | Safety review | Safety agent | Outcome |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 1 | delay 50ms | flagd | High fan_in (0.722) reveals hidden dependency; flagd is used by many services (checkout, cart, payment, etc.) so a small delay uncovers s... | Injecting a 50 ms delay into flagd will cause measurable latency spikes in checkout and cart journeys, exposing flagd as a hidden single ... | PARTIAL | cart, checkout, product | 0.44 | 0.75 | 5.0s | 46 | approved: flagd delay 50ms completed in recent runs, no aborts, tier1 light | 1 checks: 0 continue, 1 abort, 0 ignored | ABORTED by safety_agent (safety agent: product p95 221ms > baseline 132ms, outside...) |
| 2 | 1 | delay 50ms | frontend-proxy | Highest det_score and untested path; frontend-proxy sits on all journeys, so a light delay reveals hidden fan-in and latency propagation ... | A 50 ms delay in frontend-proxy will cause measurable latency increase in all four journeys (home, product, cart, checkout) without trigg... | CONFIRMED | cart, checkout, home, product | 0.57 | 1.00 | 5.0s | 60 | approved: delay 50ms on frontend-proxy is below known abort threshold (100ms) and no recent aborts at this intensity. | 2 checks: 2 continue, 0 abort, 0 ignored | COMPLETED |
| 3 | 1 | delay 50ms | cart | Highest det_score (4.211) and hits a stateful service; delay is survivable tier-1 and close to known cart crash limit, so it will reveal ... | Injecting a 50 ms delay into cart will cause measurable latency increase in the cart and checkout journeys, exposing any hidden stateful ... | CONFIRMED | cart, checkout | 0.83 | 0.50 | 5.0s | 58 | approved: 50ms delay on cart, tier1, predicted p95 ~150ms < abort 800ms, recent cart delay 50ms completed | 2 checks: 2 continue, 0 abort, 0 ignored | COMPLETED |
| 4 | 2 | delay 300ms | valkey-cart | Large delay on stateful valkey-cart probes hidden dependencies in cart/journey paths; close to known crash limit for valkey-cart, so insi... | If valkey-cart gets added network latency (delay 300ms), callers on those journeys (cart) will show errors or higher latency and journeys... | CONFIRMED | cart, checkout | 1.00 | 0.50 | 5.0s | 67 | approved: valkey-cart delay 300ms likely <800ms abort limit, no prior aborts for this intensity | - | ABORTED by watchdog (watchdog breach: cart p95 2156ms > 800ms) |

## Combined failures

Each row is one experiment with several faults applied together. Journey limits were tightened (p95 x0.8, error rate x0.8). Impact, breadth and recovery are measured once for the whole combination, and hidden dependencies are attributed to the combination, never to a single part.

| Experiment | Combination | Parts | Degraded journeys | Impact | Breadth | Recovery | Penalty | Outcome |
|---|---|---|---|---|---|---|---|---|
| exp-192041-8773 | combo: spike 50 users + delay quote 50 ms | 1. spike 50 users; 2. delay quote 50 ms | checkout | 0.26 | 0.25 | 5.0s | 22 | COMPLETED |
| exp-192201-ac56 | combo: spike 100 users + delay quote 50 ms | 1. spike 100 users; 2. delay quote 50 ms | checkout | 0.27 | 0.25 | 5.0s | 22 | ABORTED |

Vetoed by the pre-flight safety review (not injected, not counted as aborts): round 4 loss:product-catalog:10pct (product-catalog loss 10% matches known abort threshold (frontend loss 10% aborted by watchdog).)

## Earlier experiments in the database

These ran before this run and count toward service scores and hidden dependency findings.

| Experiment | Fault | Target | Degraded journeys | Impact | Breadth | Recovery | Penalty | Outcome |
|---|---|---|---|---|---|---|---|---|
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
| exp-184207-ebed | crash | image-provider | none | 0.00 | 0.00 | 5.0s | 2 | COMPLETED |
| exp-184248-3b29 | delay 50ms | flagd | cart, checkout | 0.44 | 0.50 | 5.0s | 38 | COMPLETED |
| exp-185705-ba15 | delay 50ms | recommendation | product | 0.25 | 0.25 | 5.0s | 22 | COMPLETED |
| exp-185748-7cae | spike 100 users | traffic | checkout | 0.21 | 0.25 | 5.0s | 20 | COMPLETED |
| exp-185859-be4e | spike 100 users | traffic | none | 0.00 | 0.00 | 5.0s | 2 | ABORTED |
| exp-192041-8773 | combo: spike 50 users + delay quote 50 ms | traffic+quote | checkout | 0.26 | 0.25 | 5.0s | 22 | COMPLETED |
| exp-192201-ac56 | combo: spike 100 users + delay quote 50 ms | traffic+quote | checkout | 0.27 | 0.25 | 5.0s | 22 | ABORTED |
| exp-194224-c92a | spike 100 users | traffic | none | 0.00 | 0.00 | 5.0s | 2 | COMPLETED |
| exp-194316-bd1e | spike 150 users | traffic | product | 0.22 | 0.25 | never | 39 | ABORTED |
| exp-195248-7677 | spike 100 users | traffic | none | 0.17 | 0.00 | never | 28 | ABORTED |
| exp-195618-be08 | delay 50ms | quote | checkout | 0.24 | 0.25 | 5.0s | 21 | COMPLETED |

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
| delay:astronomy-db:300ms | delay on astronomy-db aborted at 50 ms in exp-165933-0066; 300 ms excluded | exp-165933-0066 |
| delay:frontend:300ms | delay on frontend aborted at 100 ms in exp-141542-f9bb; 300 ms excluded | exp-141542-f9bb |
| loss:frontend:10pct | loss on frontend aborted at 10 % in exp-173710-08a3; 10 % excluded | exp-173710-08a3 |
| delay:product-catalog:300ms | delay on product-catalog aborted at 100 ms in exp-141653-b9e0; 300 ms excluded | exp-141653-b9e0 |

New limits learned in this run: delay 50ms on flagd aborted by safety_agent (exp-201608-51a1); delay 300ms on valkey-cart aborted by watchdog (exp-201830-5ed6).

## Hidden dependencies

None found: every degraded journey was predicted or listed in the dependency map.

## Per-service scores

| Service | Score | Worst experiment | Fault | Experiments | Weight |
|---|---|---|---|---|---|
| astronomy-db | 18.3 | exp-143142-3011 | delay 300ms | 5 | 4 |
| frontend | 18.3 | exp-141542-f9bb | delay 100ms | 2 | 4 |
| product-catalog | 18.3 | exp-164224-dcfe | delay 300ms | 2 | 4 |
| valkey-cart | 33.3 | exp-201830-5ed6 | delay 300ms | 4 | 2 |
| frontend-proxy | 39.7 | exp-201645-55d7 | delay 50ms | 5 | 4 |
| cart | 40.5 | exp-142250-d200 | crash | 3 | 2 |
| flagd | 40.5 | exp-141616-6842 | crash | 4 | 3 |
| currency | 60.2 | exp-100938-5e55 | loss 10% | 11 | 4 |
| quote | 60.2 | exp-140231-db3a | delay 100ms | 4 | 1 |
| traffic | 61.4 | exp-194316-bd1e | spike 150 users | 5 | 4 |
| traffic+quote | 77.6 | exp-192201-ac56 | combo: spike 100 users + delay quote 50 ms | 2 | 4 |
| recommendation | 78.3 | exp-185705-ba15 | delay 50ms | 1 | 1 |
| image-provider | 98.3 | exp-184207-ebed | crash | 1 | 1 |

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
- Each experiment injects one fault (container crash, added network delay, packet loss or a traffic spike), measures every journey during the fault window (up to the abort for aborted runs), reverts the fault, and measures how long the journeys take to return inside the baseline band.
- A declared combined failure injects 2 faults together as one experiment (one ledger entry per part, one watchdog, one safety review). Every journey limit is tightened for it: p95 limit x0.8, error-rate limit x0.8. It is scored once, as a whole.
- Impact, breadth, recovery and the penalty are computed deterministically from those measurements. The hypothesis verdict compares the journeys the planner predicted would degrade with those that actually did: CONFIRMED when equal, PARTIAL when they overlap, REFUTED when they do not.
- A hidden dependency is a journey that degraded by at least 0.3 although neither the hypothesis nor the dependency map expected it.
- The analyst agent ranks weaknesses and the fixer agent proposes fixes from the measured results only; their output is validated against known experiments, services and an allowlist of compose keys. When Groq is unavailable or its output is invalid, rule-based fallbacks produce the same sections. Fixes are proposals; only restart-policy and raised memory or CPU limit fixes on one allowlisted service can be applied, by `faultline fix`, and are kept only if a re-run of the same experiment improves the penalty.

## AI usage

| Agent | Path | Groq calls | Fallbacks | Cached | Prompt tokens | Completion tokens |
|---|---|---|---|---|---|---|
| planner | groq openai/gpt-oss-120b | 4 | 0 | 0 | 7795 | 2497 |
| safety | groq openai/gpt-oss-20b | 10 | 0 | 0 | 7003 | 917 |
| analyst | groq openai/gpt-oss-120b | 1 | 0 | 0 | 1586 | 2060 |
| fixer | groq openai/gpt-oss-120b | 1 | 0 | 0 | 793 | 2067 |

Safety agent: 5 checks across 3 experiments (continue 4, abort 1, ignored 0). Aborts by watchdog: 1, by safety agent: 1. Ignored checks (timeout, rate limit, invalid output or budget) leave the watchdog verdict standing.

Pre-flight review: 4 reviews of executed experiments (approve 4, reduce 0, fallback approvals 0) and 1 vetoes that skipped a candidate. A veto counts as a planner attempt, not as an abort. A failed review approves, and the validator and watchdog still apply.

Analyst weaknesses dropped by validation: 1. Fixes filled by the rule-based catalog: 0.

Recovery was not measured for exp-183210-dd26 (stopped by the operator before recovery could be measured). Their recovery component is excluded from the penalty (counted as 0), not scored as never recovered.
