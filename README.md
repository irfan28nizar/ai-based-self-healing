# AI-Based Self-Healing System - Phase 1

Phase 1 provides the microservices application that later phases will monitor,
analyse, simulate, and repair. It includes independent User, Order, and Payment
services plus controlled failure injection for demonstrations and testing.

## Services

| Service | Address | Responsibility |
| --- | --- | --- |
| User | `http://localhost:8001` | Provides user details. |
| Order | `http://localhost:8002` | Validates a user and creates orders. |
| Payment | `http://localhost:8003` | Authorizes payments for orders. |

## Start the application

```bash
docker compose up --build
```

The OpenAPI pages are available at `/docs` on each service. Stop all services
with `docker compose down`.

## Try the order flow

```bash
curl -X POST http://localhost:8002/orders \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"user-1","item":"Wireless Mouse","amount":799}'
```

## Inject and recover from a failure

Every service exposes a development control endpoint. It is only intended for
local testing; later phases will call it from the recovery engine.

```bash
# Make Payment return HTTP 503 responses.
curl -X POST http://localhost:8003/admin/failure \
  -H 'Content-Type: application/json' \
  -d '{"enabled":true,"mode":"unavailable"}'

# Restore Payment.
curl -X POST http://localhost:8003/admin/failure \
  -H 'Content-Type: application/json' \
  -d '{"enabled":false}'
```

Available modes are `unavailable` (503), `error` (500), and `slow` (delays
normal requests). Check the current state with `GET /admin/failure`.

## Next phases

Phase 2 can add Prometheus metric collection and Grafana dashboards. The
anomaly detection, root-cause analysis, digital twin, and automated recovery
modules build on the service health checks and failure controls created here.
