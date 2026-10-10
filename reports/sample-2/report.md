# Faultline resilience report

| Run | Started | Ended | Rounds | Final tier | Stop reason |
|---|---|---|---|---|---|
| run-202432-9c92 | 2026-10-10 20:24:32 UTC | 2026-10-10 20:26:36 UTC | 3/3 | 2 | completed 3 rounds |

**44.6 / 100**  
Overall resilience score (poor). Generated 2026-10-10 20:26:43 UTC.

## Executive summary

The shop's resilience is compromised by three services whose latency spikes cascade to every major user journey, creating a critical wide-blast-radius risk. Additionally, the cart, flagd, and valkey-cart services act as single points of failure: their crashes isolate the cart journey, exposing a lack of redundancy. Recovery times are within the normal floor, so the primary focus should be on reducing cross-service latency impact and adding failover or isolation for the identified single-point services.

## Ranked weaknesses and proposed fixes

Fixes are proposals. Only a config fix that maps to a restart policy or a raised memory or CPU limit on one allowlisted service can be applied, by `faultline fix` after confirmation, and it is then verified by re-running the experiment (see Fix verification). Healthcheck, replica, code and architecture changes are propose-only. Nothing is applied by generating this report.

### 1. Wide blast radius - frontend latency [CRITICAL]

Services: frontend. Experiments: exp-141542-f9bb. Source: llm.

A 100 ms delay on the frontend triggers a watchdog breach, causing all four core journeys (cart, checkout, home, product) to exceed latency SLOs, yielding impact 1.00 and breadth 1.00. The fault spreads across the entire user experience.

Evidence: exp-141542-f9bb delay 100ms on frontend: impact 1.00 breadth 1.00, degraded cart,checkout,home,product

Proposed fix (code, llm): Add timeout, retry and circuit breaker to frontend calls. Update the frontend entry code to wrap all outbound HTTP calls with a 200ms timeout. On timeout, automatically retry up to 2 times with exponential backoff (50ms, 100ms). Integrate a circuit-breaker library that opens after 3 consecutive failures and serves cached static responses for 30s before half-opening. Deploy the updated image and run smoke tests.

Propose-only (code change: propose-only).

Expected effect: Transient 100ms delays no longer trigger the watchdog breach; journeys stay within the 800ms SLO by falling back to cached data.

### 2. Wide blast radius - astronomy-db latency [CRITICAL]

Services: astronomy-db. Experiments: exp-143142-3011, exp-163723-5939. Source: llm.

Injecting 300 ms latency into astronomy-db degrades every major journey, pushing cart and checkout p95 latencies far beyond the 800 ms threshold, with impact 1.00 and breadth 1.00.

Evidence: exp-143142-3011 delay 300ms on astronomy-db: impact 1.00 breadth 1.00, degraded cart,checkout,home,product

Proposed fix (architecture, llm): Introduce read-through cache for astronomy-db. Deploy a Redis container as a read-through cache. Modify the astronomy-db client library to check Redis first; on miss, query the DB and populate the cache with a 120s TTL. Configure the service to use the cache endpoint. Verify cache hit rate improves latency under injected delays.

Propose-only (architecture change: propose-only).

Expected effect: Read queries bypass the delayed DB, keeping cart and checkout p95 latencies under the threshold despite 300ms DB latency.

### 3. Wide blast radius - product-catalog latency [CRITICAL]

Services: product-catalog. Experiments: exp-164224-dcfe, exp-141653-b9e0. Source: llm.

A 300 ms delay on product-catalog causes product and cart latency breaches, degrading all core journeys with full impact and breadth, indicating a systemic dependency on this service.

Evidence: exp-164224-dcfe delay 300ms on product-catalog: impact 1.00 breadth 1.00, degraded cart,checkout,home,product

Proposed fix (code, llm): Cache product-catalog responses in frontend. Add an in-memory LRU cache in the product-catalog client within the frontend service. Cache the full catalog payload for 60s. On each request, serve from cache if present; otherwise fetch from product-catalog with a 200ms timeout and fallback to stale cache on timeout. Deploy updated frontend image.

Propose-only (code change: propose-only).

Expected effect: A 300ms product-catalog delay is masked by cached data, preventing journey-wide latency breaches.

### 4. Single point of failure - cart service crash [HIGH]

Services: cart. Experiments: exp-142250-d200. Source: llm.

Crashing the cart service aborts the cart journey entirely (breadth 0.25) while other journeys remain unaffected, showing the cart service is a single point of failure for order flow.

Evidence: exp-142250-d200 crash on cart: impact 1.00 breadth 0.25, degraded cart only

Proposed fix (config, llm): Enable restart policy and memory limit for cart service. Create a compose override that sets restart: unless-stopped and limits memory to 512M for the cart container. Apply the override and redeploy the cart service. Verify the container restarts automatically after a crash and stays within the memory budget.

Propose-only (no compose override: propose-only).

Compose override dropped by validation: not valid YAML.

Expected effect: Cart crashes are automatically recovered, eliminating the single point of failure for the cart journey.

### 5. Single point of failure - flagd & valkey-cart crashes [HIGH]

Services: flagd, valkey-cart. Experiments: exp-141616-6842, exp-104221-fd4f. Source: llm.

Crashes of flagd and valkey-cart each abort the cart journey alone (breadth 0.25), revealing that these auxiliary services are critical single points for cart functionality.

Evidence: exp-141616-6842 crash on flagd and exp-104221-fd4f crash on valkey-cart: impact 1.00 breadth 0.25, degraded cart only

Proposed fix (architecture, llm): Add redundancy for flagd and valkey-cart. Deploy a second instance of flagd and place both behind a simple round-robin load balancer (e.g., HAProxy). Replace the single valkey-cart with a Redis master-replica pair using Redis Sentinel for automatic failover. Update service discovery to point to the load balancer and Sentinel endpoints. Test failover by killing each instance individually.

Propose-only (architecture change: propose-only).

Expected effect: Crashes of flagd or valkey-cart no longer abort the cart journey; the system continues processing with the redundant instance.

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
| 1 | 1 | delay 50ms | valkey-cart | Highest det_score (4.406) and stateful target; reveals hidden dependency on valkey-cart in checkout/cart journeys; fits tier-1 light delay. | The rank 1 pick (delay:valkey-cart:50ms) will increase latency in checkout and cart journeys, exposing the hidden stateful dependency on ... | CONFIRMED | cart, checkout | 0.63 | 0.50 | 5.0s | 48 | approved: valkey-cart delay 50ms completed in recent runs, below known abort threshold 300ms | 2 checks: 2 continue, 0 abort, 0 ignored | COMPLETED |
| 2 | 1 | crash | image-provider | Tier 1 crash of an off-journey, stateless service that hasn't been exercised; reveals hidden dependencies and single-point failures in im... | crash:image-provider:20s will cause checkout and home journeys to degrade due to missing images, exposing hidden coupling between fronten... | REFUTED | none | 0.03 | 0.00 | 5.0s | 3 | approved: Tier1 crash, no prior aborts on image-provider, abort limits 20% err, baseline 0% | 2 checks: 2 continue, 0 abort, 0 ignored | COMPLETED |
| 3 | 2 | delay 50ms | product-catalog | Light delay on the same critical service confirms latency propagation; still survivable and adds insight on hidden latency dependencies. | If product-catalog gets added network latency (delay 50ms), callers on those journeys (checkout, frontend, recommendation) will show erro... | PARTIAL | cart, checkout, product | 0.89 | 0.75 | 5.0s | 69 | approved: delay 50ms below known abort threshold (100ms), predicted p95 <800ms, no prior aborts at this intensity | - | ABORTED by watchdog (watchdog breach: product p95 1023ms > 800ms) |

## Combined failures

Each row is one experiment with several faults applied together. Journey limits were tightened (p95 x0.8, error rate x0.8). Impact, breadth and recovery are measured once for the whole combination, and hidden dependencies are attributed to the combination, never to a single part.

| Experiment | Combination | Parts | Degraded journeys | Impact | Breadth | Recovery | Penalty | Outcome |
|---|---|---|---|---|---|---|---|---|
| exp-192041-8773 | combo: spike 50 users + delay quote 50 ms | 1. spike 50 users; 2. delay quote 50 ms | checkout | 0.26 | 0.25 | 5.0s | 22 | COMPLETED |
| exp-192201-ac56 | combo: spike 100 users + delay quote 50 ms | 1. spike 100 users; 2. delay quote 50 ms | checkout | 0.27 | 0.25 | 5.0s | 22 | ABORTED |

Vetoed by the pre-flight safety review (not injected, not counted as aborts): round 3 loss:product-catalog:10pct (frontend loss 10% aborted by watchdog, product-catalog loss 10% likely breach)

## Earlier experiments in the database

These ran before this run and count toward service scores and hidden dependency findings.

| Experiment | Fault | Target | Degraded journeys | Impact | Breadth | Recovery | Penalty | Outcome |
|---|---|---|---|---|---|---|---|---|
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
| exp-201608-51a1 | delay 50ms | flagd | cart, checkout, product | 0.44 | 0.75 | 5.0s | 46 | ABORTED |
| exp-201645-55d7 | delay 50ms | frontend-proxy | cart, checkout, home, product | 0.57 | 1.00 | 5.0s | 60 | COMPLETED |
| exp-201737-6c68 | delay 50ms | cart | cart, checkout | 0.83 | 0.50 | 5.0s | 58 | COMPLETED |
| exp-201830-5ed6 | delay 300ms | valkey-cart | cart, checkout | 1.00 | 0.50 | 5.0s | 67 | ABORTED |

## Learned limits

Lowest fault intensity per target and fault kind that the watchdog or safety agent aborted in the last 7 days, as known when this run started. The planner does not propose that intensity or a higher one.

| Target | Fault | Lowest aborted intensity | Aborted by | Evidence |
|---|---|---|---|---|
| astronomy-db | delay | 50 ms | safety_agent | exp-165933-0066 |
| cart | crash | crash | watchdog | exp-142250-d200 |
| flagd | crash | crash | watchdog | exp-141616-6842 |
| flagd | delay | 50 ms | safety_agent | exp-201608-51a1 |
| frontend | delay | 100 ms | watchdog | exp-141542-f9bb |
| frontend | loss | 10 % | watchdog | exp-173710-08a3 |
| product-catalog | delay | 100 ms | watchdog | exp-141653-b9e0 |
| valkey-cart | crash | crash | watchdog | exp-104221-fd4f |
| valkey-cart | delay | 300 ms | watchdog | exp-201830-5ed6 |

Candidates excluded during this run:

| Candidate | Reason | Evidence |
|---|---|---|
| delay:astronomy-db:50ms | delay on astronomy-db aborted at 50 ms in exp-165933-0066; 50 ms excluded | exp-165933-0066 |
| delay:flagd:50ms | delay on flagd aborted at 50 ms in exp-201608-51a1; 50 ms excluded | exp-201608-51a1 |
| delay:astronomy-db:300ms | delay on astronomy-db aborted at 50 ms in exp-165933-0066; 300 ms excluded | exp-165933-0066 |
| crash:cart:20s | crash on cart aborted in exp-142250-d200; crash excluded | exp-142250-d200 |
| delay:flagd:300ms | delay on flagd aborted at 50 ms in exp-201608-51a1; 300 ms excluded | exp-201608-51a1 |
| delay:frontend:300ms | delay on frontend aborted at 100 ms in exp-141542-f9bb; 300 ms excluded | exp-141542-f9bb |
| loss:frontend:10pct | loss on frontend aborted at 10 % in exp-173710-08a3; 10 % excluded | exp-173710-08a3 |
| delay:product-catalog:300ms | delay on product-catalog aborted at 100 ms in exp-141653-b9e0; 300 ms excluded | exp-141653-b9e0 |

New limits learned in this run: delay 50ms on product-catalog aborted by watchdog (exp-202618-8de6).

## Hidden dependencies

None found: every degraded journey was predicted or listed in the dependency map.

## Per-service scores

| Service | Score | Worst experiment | Fault | Experiments | Weight |
|---|---|---|---|---|---|
| astronomy-db | 18.3 | exp-143142-3011 | delay 300ms | 5 | 4 |
| frontend | 18.3 | exp-141542-f9bb | delay 100ms | 2 | 4 |
| product-catalog | 18.3 | exp-164224-dcfe | delay 300ms | 3 | 4 |
| valkey-cart | 33.3 | exp-201830-5ed6 | delay 300ms | 5 | 2 |
| frontend-proxy | 39.7 | exp-201645-55d7 | delay 50ms | 5 | 4 |
| cart | 40.5 | exp-142250-d200 | crash | 3 | 2 |
| flagd | 40.5 | exp-141616-6842 | crash | 4 | 3 |
| quote | 60.2 | exp-140231-db3a | delay 100ms | 4 | 1 |
| traffic | 61.4 | exp-194316-bd1e | spike 150 users | 5 | 4 |
| currency | 63.8 | exp-103908-31bc | loss 10% | 7 | 4 |
| traffic+quote | 77.6 | exp-192201-ac56 | combo: spike 100 users + delay quote 50 ms | 2 | 4 |
| recommendation | 78.3 | exp-185705-ba15 | delay 50ms | 1 | 1 |
| image-provider | 97.0 | exp-202526-d3f6 | crash | 2 | 1 |

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
| planner | groq openai/gpt-oss-120b | 3 | 0 | 0 | 5931 | 1614 |
| safety | groq openai/gpt-oss-20b | 8 | 0 | 0 | 5721 | 742 |
| analyst | groq openai/gpt-oss-120b | 1 | 0 | 0 | 1610 | 1438 |
| fixer | groq openai/gpt-oss-120b | 1 | 0 | 0 | 904 | 1449 |

Safety agent: 4 checks across 2 experiments (continue 4, abort 0, ignored 0). Aborts by watchdog: 1, by safety agent: 0. Ignored checks (timeout, rate limit, invalid output or budget) leave the watchdog verdict standing.

Pre-flight review: 3 reviews of executed experiments (approve 3, reduce 0, fallback approvals 0) and 1 vetoes that skipped a candidate. A veto counts as a planner attempt, not as an abort. A failed review approves, and the validator and watchdog still apply.

Analyst weaknesses dropped by validation: 0. Fixes filled by the rule-based catalog: 0.

Recovery was not measured for exp-183210-dd26 (stopped by the operator before recovery could be measured). Their recovery component is excluded from the penalty (counted as 0), not scored as never recovered.
