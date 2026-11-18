# AI-Based Self-Healing System

The project includes the Phase 1 microservices, Phase 2 observability stack, a Phase 3 rule-based anomaly detector, and a Phase 4 root-cause analyzer. Prometheus collects service, business, and dependency-call metrics; Grafana displays service health, anomalies, and bounded diagnosis signals. The analyzer ranks evidence-backed hypotheses for human review. Automated recovery remains a later phase.

## Services and dashboards

| Component | Address | Purpose |
| --- | --- | --- |
| User service | `http://localhost:8001` | Provides user details. |
| Order service | `http://localhost:8002` | Validates a user and creates orders. |
| Payment service | `http://localhost:8003` | Authorizes payments for orders. |
| Prometheus | `http://localhost:9090` | Scrapes and stores metrics. |
| Grafana | `http://localhost:3000` | Displays the provisioned overview dashboard. |
| Anomaly detector | `http://localhost:8004` | Evaluates Prometheus signals and reports active anomalies. |
| Root-cause analyzer | `http://localhost:8005` | Ranks likely causes for active anomalies and reports supporting evidence. |

The API documentation is available at `/docs` on each service. Each service exposes Prometheus metrics at `/metrics`; the metrics endpoint stays available while application failures are injected. The detector exposes `/health`, `/anomalies`, and `/metrics`. It polls Prometheus every 10 seconds, requires a signal to persist for 20 seconds before opening an anomaly, and waits 30 seconds of normal behavior before resolving it. Active state is held in memory and clears if the detector restarts.

Phase 4 implementation details and acceptance criteria are documented in [PHASE4_PLAN.md](PHASE4_PLAN.md).

## Start the stack

```bash
docker compose up --build
```

The detector's thresholds can be changed in the `anomaly-detector` environment in `docker-compose.yml`. Initial rules flag missing or down Prometheus targets, a 5xx share of at least 25% with at least three requests in the last two minutes, p95 latency of at least one second, or at least two order dependency failures in that window.

The root-cause analyzer exposes `/health`, `/diagnoses`, and `/metrics`. It polls the detector and Prometheus every 10 seconds. `/diagnoses` returns one entry per affected service with ranked cause hypotheses, confidence bands, observed signals, PromQL, thresholds, and missing-evidence notes. Analyzer settings live in the `root-cause-analyzer` environment in `docker-compose.yml`; the default evidence window is two minutes. Its diagnosis state is in memory and clears when the analyzer restarts.

Open Grafana and sign in with `admin` / `admin` for this local demo. The **Self-Healing Demo Overview** dashboard is provisioned automatically. Change the demo password before exposing Grafana outside your local machine. Prometheus retains metrics for seven days; its data and Grafana's dashboard state use Docker volumes.

Stop the services with:

```bash
docker compose down
```

Add `-v` to `docker compose down` only if you also want to remove the stored Prometheus and Grafana data.

## Generate dashboard data

Create a successful order:

```bash
curl -X POST http://localhost:8002/orders \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"user-1","item":"Wireless Mouse","amount":799}'
```

Try a missing user to record a rejected order:

```bash
curl -X POST http://localhost:8002/orders \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"missing-user","item":"Wireless Mouse","amount":799}'
```

## Demonstrate failure signals

Every service has a local development control endpoint. For example, make Payment return HTTP 503 responses:

```bash
curl -X POST http://localhost:8003/admin/failure \
  -H 'Content-Type: application/json' \
  -d '{"enabled":true,"mode":"unavailable"}'
```

Run the order request again to generate a dependency error, then view the request error rate and latency in Grafana. Prometheus continues scraping `/metrics` while the failure is active. Restore Payment with:

```bash
curl -X POST http://localhost:8003/admin/failure \
  -H 'Content-Type: application/json' \
  -d '{"enabled":false}'
```

The other failure modes are `error` (HTTP 500) and `slow` (delays normal requests). Read the current injected state with `GET /admin/failure` on the relevant service.

To see a Phase 4 diagnosis, generate several order requests while Payment is failing, then inspect `http://localhost:8005/diagnoses`. The analyzer reports the active anomaly, leading dependency hypotheses, measured failure/latency evidence, the query window, and missing signals. `GET /health` returns `503` if the analyzer cannot read the detector or Prometheus. Grafana's **Root-cause hypotheses** and **Numeric evidence signals** panels show the bounded metric view; the API provides the full explanation.

An active diagnosis has this shape (observed values depend on the injected failure):

```json
{
  "analyzer_status": "healthy",
  "diagnoses_stale": false,
  "last_successful_evaluation": "2026-11-09T10:00:00Z",
  "window": "2m",
  "active_diagnoses": [
    {
      "service": "order-service",
      "status": "diagnosed",
      "related_anomaly_ids": ["order-service:dependency_failures"],
      "updated_at": "2026-11-09T10:00:00Z",
      "hypotheses": [
        {
          "rank": 1,
          "cause": "payment-service is failing or degraded",
          "confidence": "medium",
          "explanation": "Order-service telemetry shows elevated failures on calls to payment-service.",
          "evidence": [
            {
              "signal": "dependency failure ratio",
              "observed": 0.75,
              "threshold": ">=50%",
              "window": "2m",
              "promql": "...",
              "dependency": "payment-service",
              "unit": "ratio"
            }
          ]
        }
      ],
      "missing_evidence": []
    }
  ],
  "recent_changes": []
}
```

## Metrics included

- Request totals by service, method, route template, and status code.
- Request latency histograms and in-progress request counts.
- Order creation outcomes and successful payment authorizations.
- Prometheus scrape health for each service.

Metric labels use route templates rather than user-supplied IDs to keep metric series bounded.

Order-service dependency telemetry uses `dependency_requests_total{caller,dependency,operation,outcome}` and `dependency_request_duration_seconds`. Labels are fixed service and operation names with outcomes `success`, `http_error`, `timeout`, or `connection_error`. An expected user lookup `404` is treated as a normal dependency response. No user, order, or transaction identifiers are added to metrics. Grafana shows bounded cause/confidence and numeric evidence signals; use `http://localhost:8005/diagnoses` for explanations and PromQL details.

Diagnosis is based on aggregate metrics and temporal correlation. It cannot prove that a dependency caused a particular request failure, and no-traffic or missing telemetry yields insufficient evidence. The analyzer does not trigger restarts or other recovery actions.
