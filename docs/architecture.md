# Phase 1 Architecture

## Goal

Phase 1 establishes the application runtime that later phases will observe and make self-healing. It is intentionally a small baseline: two independent HTTP services, each running in its own container and launched by Docker Compose.

## Components

- **service-a** — minimal HTTP service listening on port 8000.
- **service-b** — minimal HTTP service listening on port 8001.
- **Docker Compose** — builds and runs both services as one local application stack.

Each service responds to `GET /` with JSON that identifies the service and current phase. They use Python's standard library HTTP server, so there are no runtime package dependencies.

## Run and inspect

Start the stack from the repository root:

```sh
docker compose up --build
```

Then call:

```sh
curl http://localhost:8000/
curl http://localhost:8001/
```

Compose places both containers on its default network, so services can address one another by their Compose service names if a later phase needs service-to-service calls.

## Phase boundaries

This phase only establishes the runtime and service packaging. It does not include telemetry collection, health monitoring, a digital-twin state model, anomaly detection, AI decision making, or automated recovery; those are planned for later phases.
