# AI-Driven Chaos Engineering for Cloud-Native Resilience

Bosch hackathon, solo build. Final pre-build overview. Nothing is implemented yet.

Project name: **on hold** ("Faultline" used below as a placeholder).

Legend: **[DECIDED]** = my decision. **[PROPOSED]** = Claude's detail, not yet confirmed. **[PENDING]** = open, blocks something.

---

## 1. Problem

Build an AI system that runs a real multi-service app in a sandbox, maps its dependencies, decides which failures are most worth testing, injects them safely with automatic stop conditions and a kill switch, measures impact and recovery, and produces an AI-generated ranked findings report with fixes and a resilience score.

| Milestone | Minimum |
|---|---|
| Day 1 (6h) | Running app, dependency map, one safe failure injection with a working kill switch |
| Day 2 | 3+ experiments, AI-generated ranked findings report with fixes, all five deliverables submitted |

Out of scope (Bosch rules): real or production systems, anything outside my sandbox, anything that affects other participants or shared infrastructure.

---

## 2. Decisions

| Area | Status | Choice |
|---|---|---|
| Language | DECIDED | Python |
| Runtime | DECIDED | Docker Compose |
| Sample app | DECIDED | OpenTelemetry Demo (Astronomy Shop), Apache 2.0, image version 3.1.0 |
| Platforms | DECIDED | Windows, macOS, Linux / GitHub Codespaces |
| AI approach | DECIDED | Hybrid: algorithm ranks, LLM ranks with reasons and explains, deterministic validator approves |
| LLM | DECIDED | Groq free API (open-weight Llama models) |
| Network faults | DECIDED | `tc netem` sidecar |
| Stand-out ideas | DECIDED | Keep all 8 (section 9) |
| Agents | DECIDED | Multi-agent inside the product: Planner, Safety, Analyst, Fixer, orchestrated as a LangGraph pipeline (section 15) |
| Safety agent | DECIDED | Live control during experiments, layered on top of the deterministic watchdog (can only make things safer) |
| Fixer | DECIDED | Auto-applies a validated compose override, re-runs the experiment, reports before/after score |
| Dev environment | DECIDED | GitHub Codespaces (4-core / 16 GB); Mac as backup |
| Scope | DECIDED | Day-1 minimum, Day-2 minimum, all three stretch goals |
| Project name | PENDING | On hold |
| Controller placement | DECIDED | Container with Docker socket mounted (section 5) |
| Docker on my Mac | DECIDED | Docker Desktop 4.94 (matches Windows judges) |
| Demo profile | DECIDED | Full stack. Measured idle use on Mac: ~3.6 GB (limits ~8.2 GB) |
| flagd failure flags | PENDING | Use as an extra fault source, or only inject infrastructure faults ourselves |
| Proposed additions | DECIDED | Section 10 confirmed as written |
| Day-2 hours and deadline | PENDING | Sets the feature freeze time |
| Windows test machine | PENDING | Is one available before submission? |

---

## 3. The sample app: OpenTelemetry Demo

Repo: `github.com/open-telemetry/opentelemetry-demo`, cloned at commit `e5552ee` (8 Oct 2026).

### Why it fits
- Around 15 app services in many languages, plus a real **Postgres** DB, **Valkey** cache, **Kafka** queue.
- Observability is built in: OTel collector, **Jaeger** traces (dynamic dependency map), **Prometheus** metrics, Grafana.
- Own Locust load generator with a web API (traffic spike control).
- flagd feature flags with built-in failure scenarios.
- Official Compose files, multi-arch images.

### Services and dependencies (from `compose.yaml`, `compose.full.yaml`, `.env`)

| Service | Port | Calls | Memory limit |
|---|---|---|---|
| frontend-proxy (Envoy, host :8080) | 8080 | frontend, image-provider, load-generator UI, Jaeger, Grafana, flagd-ui | 90M |
| frontend | 8080 | ad, cart, checkout, currency, product-catalog, recommendation, shipping | 250M |
| checkout | 5050 | cart, currency, email, payment, product-catalog, shipping, **Kafka** | 20M |
| cart | 7070 | **valkey-cart** | 160M |
| product-catalog | 3550 | **astronomy-db (Postgres)** | 20M |
| recommendation | 9001 | product-catalog | 500M |
| shipping | 50050 | quote | 20M |
| currency | 7001 | none | 20M |
| payment | 50051 | none | 140M |
| email | 6060 | none | 100M |
| ad | 9555 | none | 300M |
| quote | 8090 | none | 40M |
| image-provider | 8081 | none | 120M |
| accounting | | Kafka, Postgres | 160M |
| fraud-detection | | Kafka | 300M |
| load-generator (Locust) | 8089 | frontend-proxy | 1500M |
| astronomy-db (Postgres) | 5432 | | 80M |
| valkey-cart | 6379 | | 20M |
| kafka | 9092 | | 620M |
| flagd / flagd-ui | 8013 / 4000 | | 75M / 200M |
| otel-collector | 4317 / 4318 | | 400M |
| jaeger / prometheus / grafana / opensearch / opamp | 16686 / 9090 / 3000 / 9200 | | 1200M / 200M / 500M / 1G / 65M |

**Cross-cutting hidden dependencies:** almost every service reads **flagd** and sends telemetry to **otel-collector**. These do not appear in the request path but can still hurt it. Good candidates for "predicted vs actual blast radius".

### Resource budget
- Sum of memory limits for `make start` (core + full + observability + extras): **about 8.2 GB**, before our own containers.
- Windows WSL2 defaults to about 50% of host RAM. On a 16 GB Windows laptop that is about 8 GB: **too tight for the full stack.**
- Options [PENDING]: full stack and require a `.wslconfig` bump, or a trimmed profile (drop OpenSearch, Grafana, opamp; about 6.4 GB), or `start-minimal` (drops Kafka, accounting, fraud-detection; loses the async pipeline).

### Things to fix in our setup
- `.env` has `DEMO_VERSION=latest`: images are **not pinned**. We pin to `3.1.0` in our override so every run (Mac, Windows, Codespaces) uses identical images.
- The demo's `Makefile` needs `make` and creates `.env.override`. Windows has no `make` by default, so our CLI calls `docker compose` directly with the same files and env files.
- `compose.agent.yaml` (AI agent, MCP, chatbot) and profiling are not used.

---

## 4. Architecture

```
            +------------------- Dashboard (FastAPI + SSE) -------------------+
            | dependency map | live metrics | experiment log | scores | KILL  |
            +------------------------------^----------------------------------+
                                           |
 +--------+  +---------+  +-----------+  +-+--------+  +----------+  +----------+
 | Mapper |->| Planner |->| Validator |->| Injector |->| Analyzer |->| Reporter |
 +---+----+  +----+----+  +-----------+  +----+-----+  +----+-----+  +----------+
     ^            | Groq (optional,           |             ^
     |            v  fallback if absent)      v             |
     |                              Fault ledger (write-ahead)
     |                                  |            |
     |              Safety watchdog <---+            +--> Reaper (dead-man's switch,
     |              (abort on breach)                     separate process)
     |                                                          |
 +---+---------------------------- Observer ----------------------+
 | journey prober (independent) | Prometheus | Jaeger | Docker events/stats | Locust |
 +----------------------------------------------------------------+
                               |
              OpenTelemetry Demo (Docker Compose, pinned 3.1.0)
```

### Mapper
- **Static:** parse `*_ADDR` / `*_HOST` env vars and `depends_on` from the compose files into a directed graph.
- **Dynamic:** pull the service graph from Jaeger traces (`/api/dependencies`) to confirm edges and find ones the static parse missed (flagd, collector, Kafka consumers).
- **Journeys:** home page, product page, add to cart, checkout. Each mapped to the services it needs.
- **Manual corrections:** `map.overrides.yaml`.
- Output: graph JSON + rendered diagram. Experiments feed newly observed edges back into the map.

### Observer
- **Independent synthetic prober** hitting real journeys through frontend-proxy every second. This is the ground truth, because it does not depend on the app's own telemetry pipeline.
- **Prometheus** queries for per-service error rate and latency (via OTel span metrics).
- **Jaeger** for traces and actual blast radius.
- **Docker events** (die, restart, health) and `docker stats`.
- **Locust** stats from the load generator API.
- If otel-collector or Prometheus is degraded, the prober still works. If the prober itself goes blind, the watchdog aborts.

### Baseline
- Warm-up discarded, then N minutes of steady Locust load.
- Per journey and per service: error rate, p50, p95, stddev.
- Thresholds: `max(absolute_floor, baseline + k * sigma)`. No hard-coded numbers.

### Planner (hybrid)
1. Generate candidates: target x fault x intensity from the fault catalog.
2. Deterministic score: graph centrality, number of journeys depending on the target, fan-in, statefulness, single replica, untested, results so far (adaptive).
3. Send the top ~8 to Groq with a compact map, baseline and history.
4. LLM returns strict JSON: **full ranked list, each with a short reason**, plus for the top pick a **hypothesis** and **predicted blast radius**.
5. Ranked plan saved and shown in report and dashboard, with deterministic score next to LLM rank.
6. No key, timeout, 429, or invalid JSON: one retry, then deterministic top-1 with a template hypothesis. The report states which path was used.

### Validator (guardrails)
- Targets: app services and data stores only. Never: our controller, reaper, prober, dashboard, Locust (except for spike), otel-collector, Prometheus, Jaeger.
- Fault type in catalog, intensity and duration within caps.
- One experiment at a time, except declared combined-failure mode.
- No identical repeat, total experiment budget per run.
- Pydantic schema check. Every rejection logged with its reason.

### Injector: fault catalog

| Fault (Bosch list) | Target in OTel demo | Mechanism | Revert |
|---|---|---|---|
| Service or container crash | any app service | `docker kill` (SIGKILL) | restart policy / `docker start` |
| Service hang (extra) | any app service | `docker pause` | `docker unpause` |
| Slow or lossy network | any app service | `tc netem` from a sidecar sharing the target's network namespace (`NET_ADMIN`) | `tc qdisc del` |
| Full disk | astronomy-db (Postgres) or Kafka | fill a size-capped tmpfs mounted at the data dir | delete the fill file |
| Overloaded database | astronomy-db | CPU throttle (`docker update --cpus`) and/or query flood from a sidecar | restore limits, stop flood |
| Failed dependency | Kafka, quote, flagd, valkey | blackhole caller-to-dependency traffic (timeouts) or stop the dependency (refused) | remove rule / start |
| Sudden traffic spike | whole app | Locust API: raise user count | back to baseline users |
| App-level faults (optional) | various | flagd flags, e.g. `paymentFailure`, `cartFailure`, `productCatalogLockContention`, `kafkaQueueProblems` | flag back to `off` |

Every fault is written to the **ledger before it is applied**: id, target, revert action, `expires_at`.

### Safety watchdog (every second)
Abort if any of:
- journey error rate over threshold
- p95 latency over threshold
- blast radius beyond the allowed set
- **prober metrics missing for more than 5s** (blind is unsafe, never "healthy")
- host free disk or Docker memory below a floor

On abort: revert, verify revert, mark `ABORTED`, cooldown.

### Kill switch
All paths run the same idempotent revert-all:
- CLI `faultline stop`
- dashboard button
- Ctrl+C
- `STOP` sentinel file

### Reaper (dead-man's switch)
- Separate process, reads the ledger.
- Reverts any fault past `expires_at`, or everything if the controller heartbeat is stale (over 10s).
- On controller startup: reconcile and revert leftovers from a previous crash.
- Demo: force-kill the controller mid-experiment; the reaper still cleans up.

### Analyzer
- Impact per journey and service vs baseline.
- Actual vs predicted blast radius. Mismatch = **hidden dependency finding**, fed back to the map.
- Recovery time: from fault end until back in the baseline band. Crash: also time to restart. Never recovered = flagged.
- Hypothesis verdict: confirmed or refuted, with evidence.

### Resilience score (explainable)
- Per experiment: `penalty = 100 x (0.5 x Impact + 0.3 x Breadth + 0.2 x Recovery)`
  - Impact: worst normalized degradation across journeys (0..1)
  - Breadth: affected critical journeys / all critical journeys
  - Recovery: `min(1, recovery_seconds / 60)`, never recovered = 1
- Service score = `100 - worst penalty` on that service.
- Overall = criticality-weighted mean. Formula printed in the report.

### Reporter
- `report.md`, `report.html`, `report.json`.
- Ranked weaknesses by severity, evidence charts, hypothesis verdicts, hidden dependencies, recommended fixes, scores, and which path (LLM or fallback) produced it.
- Fixes: written by Groq from evidence. Fallback: rule-based fix catalog.

### Stretch goals
- **Fix loop:** AI writes `compose.fix.yaml` limited to allowed keys (restart policy, healthcheck, resource limits). Validated with `docker compose config`, applied, same experiment re-run, before/after score.
- **Live dashboard:** FastAPI + Server-Sent Events + plain JS (Chart.js, Cytoscape.js).
- **Combined failures:** declared combinations, e.g. traffic spike while payment is down. Tighter thresholds.

---

## 5. Cross-platform design (Windows, macOS, Codespaces)

All containers run Linux on every platform (Docker Desktop on Windows uses WSL2), so the chaos mechanisms are identical everywhere. The differences are on the host.

| Issue | Handling |
|---|---|
| No `make` on Windows | Our Python CLI calls `docker compose` directly. No make or bash needed |
| CRLF line endings break scripts inside Linux containers | `.gitattributes` forcing LF |
| No POSIX signals on Windows | Ctrl+C everywhere; reaper demo uses `taskkill /F` on Windows, or is moot if the controller runs in a container |
| WSL2 memory cap (~50% of RAM) | Startup check of Docker's available memory, with `.wslconfig` instructions or trimmed profile |
| Windows reserved port ranges | All host ports configurable, clear error on bind failure |
| SQLite on Windows bind mounts | Ledger in a Docker volume or a local path, never a cross-OS bind mount |
| Paths | `pathlib` only |
| `tc netem` kernel support | Verified early on Mac, Codespaces, and Windows if a machine is available |
| arm64 (Mac) vs amd64 (Windows, Codespaces) | Demo images are multi-arch; our images built multi-arch |

**[PENDING] Controller placement**

| Option | Pros | Cons |
|---|---|---|
| Host Python process | Easier to debug while building | Judges need Python; signal and kill behavior differs per OS |
| Container with Docker socket mounted | Judges need only Docker; identical on all OSes; reaper is just another container | Docker socket gives full control of the Docker host (acceptable in a sandbox, must be documented); slower debugging |

---

## 6. Experiment lifecycle

```
PROPOSED -> VALIDATED -> (REJECTED)
         -> PREFLIGHT (at baseline? no active faults?)
         -> INJECTING -> OBSERVING -> REVERTING -> RECOVERING -> COMPLETED
                      \-> ABORTED (threshold / kill switch / blind) -> REVERTING
```

Preflight refuses to start unless the system is back at baseline, so one experiment's damage is never blamed on the next.

---

## 7. Running it

```
faultline up        # start the demo (pinned) + our stack
faultline map       # build dependency map
faultline baseline  # capture steady state
faultline run       # plan -> validate -> inject -> observe -> analyze
faultline stop      # kill switch
faultline report    # findings report
faultline demo      # end to end
```

**Codespaces:** `devcontainer.json` with docker-in-docker, 4 CPU / 16 GB host requirements, forwarded ports (8080 shop, dashboard), `GROQ_API_KEY` as a recommended secret. Judges without a key get the deterministic fallback.

---

## 8. Edge cases

**Safety and cleanup**
- Controller crash mid-fault: reaper + `expires_at` + startup reconciliation.
- Kill switch during injection: idempotent revert, handles partial state.
- Revert fails: retry, then `REVERT_FAILED`, no new experiments.
- Target restarts during a network fault: netem rule disappears with the namespace; logged, not counted as recovery.
- Full disk only on a capped tmpfs, never the host disk.
- CPU and load faults capped so the laptop stays usable.

**Measurement**
- Missing metrics = unsafe.
- Breaking otel-collector or Prometheus would blind the app's own telemetry: excluded from targets, and the independent prober is the ground truth.
- Noisy baseline: warm-up discard, sigma thresholds, minimum sample counts.
- Restart policies hiding crashes: recovery measured from events and journey health.
- Preflight gate before every experiment.
- One monotonic clock in the controller.
- Kafka is async: checkout can look healthy while orders pile up. Consumer lag and accounting writes are checked, not only HTTP success.

**AI and LLM**
- Non-existent or forbidden target: rejected, logged.
- Invalid JSON: one retry, then fallback.
- 429 or timeout: backoff honoring `retry-after`, then fallback.
- No key: deterministic path, disclosed in the report.
- Key never in logs, report, dashboard, or the public repo.
- Compact prompts for free-tier token limits.

**Environment**
- Unpinned `latest` images: pinned to 3.1.0.
- Docker not running, low memory, port conflicts: clear messages, no stack traces.
- First image pull is several GB: done before any timed demo.

---

## 9. Stand-out features (all 8 kept)

1. Hypothesis-first experiments with confirmed / refuted verdicts.
2. Predicted vs actual blast radius, hidden dependency discovery (flagd and otel-collector are prime candidates).
3. "AI proposes, guardrails dispose": validator with logged rejections.
4. Dead-man's-switch reaper with a force-kill demo.
5. Closed-loop fix verification with before / after score.
6. Adaptive planning across rounds.
7. Explainable resilience score.
8. One-command judge experience on Codespaces, Windows and Mac.

---

## 10. Additions (confirmed)

| Detail | Proposal | Why it matters |
|---|---|---|
| Storage | SQLite for ledger and time series | Survives a controller crash, which the reaper depends on |
| Groq model | `llama-3.3-70b-versatile`, fallback `llama-3.1-8b-instant` | Quality for planning, cheap fallback under limits |
| Dashboard stack | FastAPI + SSE + plain JS | No frontend build step |
| Score weights | 0.5 / 0.3 / 0.2 | Impact weighted highest, tunable |
| netem sidecar image | `nicolaka/netshoot` (multi-arch, has `tc`) | No custom image to build |
| Demo source | Our repo pins the OTel demo at a fixed commit/tag (submodule or fetch script) instead of copying it | Keeps our repo small and the licence boundary clear |

---

## 11. Build plan

**Day 1 (6h)**

| Time | Work | Milestone |
|---|---|---|
| 0:00 - 0:45 | Install Docker, my repo, pull and start the demo, measure RAM | Setup and app selection |
| 0:45 - 1:45 | Our CLI wrapper (`up`/`down`, pinned images), journey prober | Setup, cross-platform |
| 1:45 - 2:30 | Mapper (static + Jaeger) and map output, baseline capture | Mapping, monitoring baseline |
| 2:30 - 4:00 | Injector (crash + netem delay), ledger, reaper, kill switch | First failure injection |
| 4:00 - 5:00 | Safety watchdog, deliberate threshold-breach test | Safety controls |
| 5:00 - 6:00 | Day-1 demo script, buffer | Day-1 checkpoint |

**Day 2** (order protects the minimum)

| Phase | Work | Milestone |
|---|---|---|
| A | Planner + Groq + validator + fallback, remaining faults, 3+ fault types run, analyzer, score, AI-generated report. Commit the Groq-generated sample report early | AI planner, multiple experiments, analysis and scoring |
| B | Dashboard, fix loop, combined failures (only after A works) | Stretch goals |
| C | Feature freeze. Bug fixes, README, clean run from scratch on Codespaces and a fresh clone | Freeze and polish |
| D | Demo video (3 min or less), architecture one-pager, final findings report | Video and documents |
| E | Team form, open every deliverable from a logged-out browser, submit 30 min early | Submission |

---

## 12. Milestone traceability

| Official milestone | Satisfied by |
|---|---|
| Setup and app selection | Pinned OTel demo + `faultline up` + prober green |
| Application mapping | Static + Jaeger mapper, `map.overrides.yaml` |
| Monitoring baseline | Observer + Locust + `faultline baseline` |
| First failure injection | Crash and netem delay |
| Safety controls | Watchdog + deliberate breach test |
| Day-1 checkpoint / minimum | Day-1 demo script, kill switch, reaper |
| AI experiment planner | Ranked list with per-candidate reasons |
| Multiple experiments | 7+ fault types, recovery time recorded |
| Analysis and scoring | Analyzer, Groq explanations, resilience score |
| Feature freeze and polish | Day-2 phase C |
| Demo video and documents | Day-2 phase D |
| Submission | Day-2 phase E |
| Day-2 minimum | Phase A + committed AI-generated report + phases D, E |

---

## 13. Deliverables

| # | Deliverable | Content |
|---|---|---|
| 1 | Source code | Public repo, README with setup for Windows, Mac and Codespaces |
| 2 | Demo video | 3 min or less: injection, impact, report |
| 3 | Architecture overview | One page: components, how the AI decides, safety controls |
| 4 | Findings report | Groq-generated sample run, ranked, with fixes |
| 5 | Team form | Name, tools, AI models (Groq Llama, plus Claude used for development), third-party libraries, data |

---

## 14. Risks

| Risk | Mitigation |
|---|---|
| RAM: full stack about 8.2 GB, WSL2 default cap about 8 GB | Trimmed profile or `.wslconfig`; startup memory check |
| First pull of several GB on venue Wi-Fi | Pull images as the very first step |
| Groq free-tier limits (unofficial: ~30 req/min, ~12k tokens/min, ~1k req/day for 70B) | Few batched calls, caching, fallback, sample report committed early |
| `tc netem` unsupported in some Docker VM kernel | Test on each platform early; fall back to flagd delay flags or Toxiproxy for that platform |
| No Windows machine to test on | Codespaces covers Linux; Windows-specific risks handled by design (section 5) |
| Public repo leaks the Groq key | Env / Codespaces secret only, `.env` gitignored, scan before submission |
| Scope too large for solo | Minimum first, each stretch goal an independent layer |

---

## 15. Multi-agent design (confirmed)

Four LLM agents, orchestrated as a LangGraph `StateGraph` pipeline. Everything safety-critical stays deterministic underneath.

```
 map + baseline (deterministic)
        |
   [Planner agent]  ranked candidates, reason each, hypothesis, predicted blast radius
        |
   validator (deterministic guardrails)
        |
   inject (deterministic)
        |
   observe loop: every 1s  -> deterministic watchdog (hard floor)
                 every ~10s -> [Safety agent]: continue / abort + reason
        |
   revert + recovery (deterministic)
        |
   [Analyst agent]  impact, actual vs predicted blast radius, verdict, severity rank
        |
   [Fixer agent]    concrete fix per finding -> validated compose override -> re-run -> before/after
        |
   next round (adaptive) or report
```

**Safety agent rules**
- Can only make things safer: abort early or shorten. Never cancels a watchdog abort, raises thresholds, or extends duration.
- Fixed cadence (~10s), not every tick, to fit free-tier rate limits.
- Timeout, 429 or invalid output = watchdog verdict stands, logged. Never treated as "continue".

**All agents**
- Typed Pydantic outputs; one retry on invalid output, then that agent's deterministic fallback.
- No key: Planner = deterministic ranker, Safety = watchdog only, Analyst and Fixer = rule-based templates. Report states which agents ran in fallback.
- Shared rate-limit token bucket (~25 req/min) with Safety prioritised.
- Agent trace (input summary, output, latency, fallback flag) stored in SQLite, shown in report and dashboard.
- Models: Planner, Analyst, Fixer on `llama-3.3-70b-versatile`; Safety on `llama-3.1-8b-instant`. Client: `langchain-groq`.

**Fixer auto-apply guardrails**
- Override limited to allowed keys (restart policy, healthcheck, resource limits).
- Validated with `docker compose config` before applying.
- Applied fix is recorded in the ledger so it can be rolled back; original state restorable with one command.

**Timing:** agents and LangGraph land on Day 2 phase A. Day-1 work is unchanged.
