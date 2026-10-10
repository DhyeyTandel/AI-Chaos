# AI-Chaos

Faultline: AI-driven chaos engineering for the OpenTelemetry Demo (Astronomy Shop), pinned to 3.1.0.

## Prerequisites

Docker (Docker Desktop or Engine with Compose v2). Nothing else.

## Quick start

```
git clone --recurse-submodules <repo-url> && cd AI-Chaos
docker compose up -d
docker compose run --rm faultline status
docker compose run --rm faultline probe --seconds 30
```
