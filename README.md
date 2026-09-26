# AI-Based Self-Healing Software System Using Digital Twin

An AI-driven self-healing software system that will monitor a microservices-based application, maintain a digital twin of its runtime state, detect failures, and evaluate recovery actions before applying them.

## Project Overview

Modern distributed applications can experience service crashes, high latency, resource exhaustion, dependency failures, and abnormal runtime behavior. Manual detection and recovery can increase downtime and operational effort.

This project is being built incrementally. The first phase establishes a small containerized application runtime that later phases can monitor and use to develop the self-healing workflow.

## Phase 1: Core Architecture and Initial Microservices

Phase 1 provides two independent HTTP services, each packaged as a Docker image and started together with Docker Compose. Both services expose a simple JSON response at `/` so the baseline runtime can be started and inspected.

### Run locally

Requirements: Docker with the Compose plugin.

```sh
docker compose up --build
```

In another terminal, request each service:

```sh
curl http://localhost:8000/
curl http://localhost:8001/
```

Stop the services with `Ctrl+C`. Run `docker compose down` to remove the containers.

### Phase 1 structure

```text
.
├── docker-compose.yml
├── docs/
│   └── architecture.md
└── services/
    ├── service-a/
    │   ├── Dockerfile
    │   └── app.py
    └── service-b/
        ├── Dockerfile
        └── app.py
```

The services use Python's standard library and need no Python package installation. See [docs/architecture.md](docs/architecture.md) for the current architecture and phase boundaries.

## Architecture

The target system has four major planes:

1. **Application Runtime Plane** — microservices and supporting runtime components.
2. **Observability Plane** — metrics, logs, health checks, events, and runtime telemetry.
3. **Digital Twin & Intelligence Plane** — digital-twin state, anomaly/failure analysis, and AI-based decision support.
4. **Recovery / Execution Plane** — recovery orchestration, execution, and verification.

Target self-healing flow:

```text
Application Runtime
        ↓
Observability
        ↓
Digital Twin & Intelligence
        ↓
AI Engine
        ↓
Self-Healing Orchestrator
        ↓
Recovery Controller
        ↓
Microservices
        ↺
Continuous Feedback Loop
```

## Development Roadmap

- [x] Phase 1 — Core architecture and initial microservices setup
- [ ] Phase 2 — Observability and health monitoring
- [ ] Phase 3 — Digital Twin state model
- [ ] Phase 4 — Failure and anomaly detection
- [ ] Phase 5 — AI Engine
- [ ] Phase 6 — Self-Healing Orchestrator
- [ ] Phase 7 — Recovery Controller and automated recovery
- [ ] Phase 8 — Feedback loop and recovery verification
- [ ] Phase 9 — Testing, evaluation, and final documentation

## Project Status

The project is being developed incrementally as an industry-oriented implementation of an **AI-Based Self-Healing Software System Using Digital Twin**. Each completed phase will be reflected in source code and documentation.

## Academic Project

**Project Title:** AI-Based Self-Healing Software System Using Digital Twin

This repository contains the implementation, documentation, experiments, and supporting material for the project.
