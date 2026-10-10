# AI-Chaos (Faultline)

AI-driven chaos engineering for cloud-native resilience. Faultline maps a running microservice application, uses an LLM-assisted planner to choose which faults are worth injecting, injects them under several independent safety layers, measures what breaks, and writes a ranked resilience report with proposed fixes. The application under test is the OpenTelemetry Demo (Astronomy Shop) 3.1.0 (about 26 containers), pinned as a git submodule. Everything starts with one `docker compose up -d`.

Built for the Bosch hackathon "AI-Driven Chaos Engineering for Cloud-Native Resilience". For the design in one page, see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## What it does

1. **Map.** Builds a service dependency map from the Compose files plus live Jaeger traces, and derives four user journeys (home, product, cart, checkout). Flags dependencies seen only in traces or only in config.
2. **Plan.** Scores every candidate fault deterministically (fan-in, journeys affected, stateful, untested, recent results, severity). The planner remembers across runs the lowest intensity per target and fault kind that the watchdog or safety agent aborted, and does not propose it again. An LLM ranks the top 8 with a reason, a hypothesis and a predicted blast radius. A validator rejects unsafe picks, and the safety agent reviews the pick before injection (approve, reduce or veto).
3. **Inject safely.** Container crashes and network delay, jitter and packet loss, each recorded in a write-ahead ledger before being applied, watched every second, and reverted automatically.
4. **Observe.** A prober runs the four journeys continuously against a measured baseline. Recovery time is measured after each revert.
5. **Report.** An analyst agent ranks weaknesses with evidence, a fixer agent proposes fixes, and a resilience score is computed. Output is `report.md`, `report.html` and `report.json`.

## Quick start

Requirements: Docker with Compose v2, and about 4 CPU / 16 GB RAM for the full stack. No other tooling is needed on the host.

**GitHub Codespaces**

Open the repository in a Codespace (the devcontainer requests 4 CPU / 16 GB and pulls the submodule). Then:

```bash
docker compose up -d
```

Port 8080 is forwarded and labelled "Astronomy Shop".

**Windows (Docker Desktop), macOS (Docker Desktop), Linux (Docker Engine)**

```bash
git clone --recurse-submodules https://github.com/DhyeyTandel/AI-Chaos.git AI-Chaos
cd AI-Chaos
docker compose up -d
```

If you cloned without `--recurse-submodules`, run `git submodule update --init --recursive`.

**Wait, then check**

The first start pulls images and takes several minutes. Then:

```bash
docker compose run --rm faultline status
```

`status` exits 0 when every container is running and none is unhealthy.

| URL | What |
|---|---|
| http://localhost:8080 | Astronomy Shop |
| http://localhost:8080/jaeger/ui/ | Jaeger traces |
| http://localhost:8080/loadgen/ | Locust load generator |

**Ports and bind address** (optional, in a root `.env` file): `FAULTLINE_BIND` (default `0.0.0.0`), `SHOP_PORT` (8080), `ENVOY_ADMIN_HOST_PORT` (10000), `PROMETHEUS_HOST_PORT` (9090).

### Optional: Groq API key

Get a free key at console.groq.com. Put `GROQ_API_KEY=...` in a root `.env` file (gitignored), or set it as a Codespaces secret named `GROQ_API_KEY`. Without a key, every agent uses its deterministic fallback and the tool still runs end to end.

## Commands

All commands run as `docker compose run --rm faultline <command>`.

| Command | Purpose |
|---|---|
| `status` | List containers with state, health, restarts, OOM kills |
| `probe --seconds N` | Run the journey prober and print per-journey success and latency |
| `map` | Build the dependency map and journeys, print hidden-dependency candidates and a Mermaid graph |
| `baseline [--seconds N] [--warmup N] [--show]` | Measure steady state and derive watchdog thresholds |
| `experiment --fault crash\|delay --target SVC --duration S [--delay-ms] [--jitter-ms] [--loss-pct]` | One guarded fault: pre-flight safety review, then watchdog, revert and recovery measurement |
| `plan [--top N]` | Rank candidate faults and validate the pick, without running it; lists candidates excluded by learned limits |
| `run [--rounds N] [--report]` | Autonomous loop: plan, validate, execute, record, climb the risk ladder (1 to 8 rounds, default 3) |
| `runs show [RUN_ID]` | Print a stored run with hypotheses, pre-flight reviews, vetoes, exclusions and safety-agent decisions |
| `report [--run RUN_ID]` | Analyze a stored run and write the report files under `reports/<run_id>/` |
| `ledger [-n N]` | Show the fault ledger |
| `stop` | Kill switch: revert every open fault and remove sidecars |
| `agents trace` | Recent agent calls: model, latency, tokens, fallback, input and output |
| `agents usage` | Today's per-model requests and tokens against the daily budget, plus cache hits |

## A 3-minute tour

```bash
docker compose run --rm faultline status
docker compose run --rm faultline map
docker compose run --rm faultline baseline
docker compose run --rm faultline run --rounds 3 --report
```

Then open `reports/<run_id>/report.html` (the folder is mounted from the repo, so it appears on the host), and browse the traces of the faulted requests in Jaeger at http://localhost:8080/jaeger/ui/. A finished example is in [reports/sample/](reports/sample/).

## Safety model

Faults are never applied without these layers in place. Each is independent of the LLM.

| Layer | What it does |
|---|---|
| Preflight | Refuses to start unless the system is within baseline thresholds |
| Target allowlist | Only listed application services are targetable; observability and tooling never are; `frontend-proxy` cannot be crashed |
| Parameter caps | Max duration 120 s, delay 2000 ms, jitter 500 ms, loss 30 % |
| Write-ahead ledger | The fault is recorded in SQLite before it is applied, so anything left over can be found and reverted |
| Watchdog | Every 1 s, judges each journey against thresholds derived from the baseline (error rate and p95, with floors in `faultline.toml`); aborts on breach, or if no samples arrive for 5 s |
| Pre-flight safety review | Before injection the safety agent approves, reduces (only lowers delay, loss or duration; a crash can only get shorter, at least 10 s) or vetoes the planned fault. A reduction is re-checked by the validator and treated as a veto if invalid. A failed or timed-out review approves, because the validator and watchdog still apply |
| Cross-run planner memory | The lowest intensity per target and fault kind that the watchdog or safety agent aborted in the last 7 days is never proposed again (`[planner.memory]`). Aborts by the operator, preflight, inject or system are ignored |
| Safety agent | Live advisory check about every 10 s; can only abort earlier, never override the watchdog |
| Kill switch | `faultline stop` reverts everything and writes a STOP sentinel that running experiments honour |
| Reaper | Separate always-on container; reverts faults past expiry, or all faults if the controller heartbeat is older than 10 s (measured live: 13 s) |
| Self-expiring netem sidecar | The network fault removes its own rule when its TTL ends (verified with both controller and reaper stopped) |

Crashes hold the container restart policy at `no` during the fault and restore it afterwards.

## Fault catalog

| Fault | Mechanism | Status |
|---|---|---|
| Container crash | Stop container; restart policy held at `no`, restored after | Implemented |
| Network delay and jitter | `tc netem` from a `nicolaka/netshoot` sidecar in the target's network namespace | Implemented |
| Packet loss | Same sidecar, netem loss | Implemented |
| Traffic spike | Locust UI or API | Manual (not driven by the planner) |
| Full disk | | Future work |
| Database overload | | Future work |

## AI agents

LangGraph pipeline; models are served by Groq's free tier.

| Agent | Model | Decides | Fallback without LLM |
|---|---|---|---|
| Planner | `openai/gpt-oss-120b` (reasoning effort low) | Ranks the top 8 scored candidates, with a reason, hypothesis and predicted blast radius; sees the learned limits | Deterministic score order |
| Validator | none (rules) | Rejects candidates that break the safety rules or run while faults are active | Not an LLM |
| Analyst | `openai/gpt-oss-120b` | Ranks weaknesses with evidence and severity | Penalty-ranked weaknesses |
| Fixer | `openai/gpt-oss-120b` | Proposes fixes, including validated Compose overrides (proposed only, never applied) | Fallback fixes |
| Safety agent | `openai/gpt-oss-20b` (reasoning effort low) | Pre-flight review (approve, reduce or veto) before injection; live continue or abort advice during a fault | Review approves; live checks ignored; watchdog verdict stands |

The planner climbs a risk ladder: tier 1 (50 ms delays, crashes of services no journey needs), tier 2 (300 ms delay, 10 % loss, partial-impact crashes, core-path delays), tier 3 (critical or stateful crashes, including flagd). It climbs after 2 survived experiments at a tier, and a target that triggers an abort is excluded.

Cross-run memory: before each plan, `faultline` reads the experiments table and builds the known limits. Candidates at or above a known abort intensity for the same target and fault kind are excluded with a recorded reason, shown by `plan` and `run`, and listed with their evidence experiment ids in the report's "Learned limits" section. A pre-flight veto skips the candidate and asks for the next ranked one; it counts as a planner attempt, not as an abort, and does not exclude the target.

Token budget handling: a per-model limiter (8000 tokens/min), a daily budget per model (180,000 tokens), a 6 h response cache, and `agents usage` to inspect it. Calls fall back when a limit is hit.

## Sample results

From live runs on a Codespace-sized host (see `reports/sample/` for a full report):

- **Risk ladder.** One run climbed from tier 1 to tier 2: 50 ms delays on astronomy-db and frontend-proxy completed, then a crash of cart was aborted by the watchdog at cart p95 3007 ms (recovery 6 s).
- **Hidden dependency.** Crashing flagd, which is on no journey in the map, pushed cart p95 to about 3 s.
- **Latency without errors.** 10 % packet loss on currency raised checkout p95 from 74 ms to 410 ms with zero errors.
- **Pre-flight review and memory.** In the sample run the planner already knew six learned limits (for example delay on product-catalog aborted at 100 ms), the safety review vetoed one candidate before injection and approved the other three, and a 50 ms delay on astronomy-db was then aborted by the live safety agent because checkout p95 rose to 285 ms against a 74 ms baseline.
- **Score.** The sample report scores the shop 37.5 / 100 overall. Per experiment, penalty = 100 x (0.5 impact + 0.3 breadth + 0.2 recovery); a service scores 100 minus its worst penalty; the overall score is the mean of service scores weighted by the number of journeys each service sits on.

## Status

| Area | Status |
|---|---|
| Dependency mapper, baseline, prober | Done |
| Crash, delay, jitter and loss faults | Done |
| Ledger, watchdog, kill switch, reaper, netem expiry | Done, tested live |
| Planner, validator, risk ladder, autonomous run | Done |
| Analyst, fixer, resilience score, Markdown/HTML/JSON report | Done |
| Token budget, cache, agent trace | Done |
| Safety agent (live advisory) | Done (gpt-oss-20b, verified live) |
| Pre-flight safety review (approve, reduce, veto) | Done |
| Cross-run planner memory and learned limits | Done |
| Auto-apply of proposed fixes | Future |
| Dashboard | Future |
| Combined (multi-fault) failures | Future |
| Full-disk and database-overload faults | Future |
| Planner-driven traffic spikes | Future |

## Repository layout

```
compose.yaml            includes the pinned demo, adds the faultline and reaper services
compose.override.yaml   resource and port tweaks to the demo without editing vendor/
faultline.toml          thresholds, safety caps, allowlist, planner, LLM and score settings
map.overrides.yaml      manual corrections to the dependency map
config/                 OpenTelemetry Collector trim config
src/faultline/          CLI, mapper, prober, watchdog, injector, ledger, reaper, analyzer, report
src/faultline/agents/   planner, validator, analyst, fixer, safety
tests/                  test suite
docs/                   OVERVIEW.md (full design), ARCHITECTURE.md (one-page summary)
reports/                generated reports; reports/sample/ is a committed example
vendor/opentelemetry-demo/   upstream demo 3.1.0 (git submodule)
.devcontainer/          Codespaces configuration
```

## Credits and license

The application under test is the [OpenTelemetry Demo](https://github.com/open-telemetry/opentelemetry-demo), Apache 2.0. The approach follows the principles of chaos engineering: define steady state, form a hypothesis, inject realistic faults, and limit the blast radius.

License: MIT (see [LICENSE](LICENSE)). The OpenTelemetry Demo in `vendor/opentelemetry-demo` keeps its own Apache 2.0 license.
