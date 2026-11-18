# Phase 3 Plan: Anomaly Detection

## Goal

Add a detector that notices abnormal service behavior from the metrics Phase 2 already collects, records an alert state, and makes that state visible in Grafana. Phase 3 diagnoses that something is wrong; it does not yet decide the root cause or change service behavior.

## Current baseline

- Three FastAPI services run in Docker Compose: user, order, and payment.
- Each service exports request totals, request duration, in-progress requests, business outcomes, and `/health` through `/metrics`.
- Prometheus scrapes every five seconds and retains seven days of data.
- Grafana has a provisioned overview dashboard.
- Failure injection can produce HTTP 500, HTTP 503, and slow requests. The controls are local development endpoints.
- There is no detector, alert state store, notification path, or recovery controller yet.

## Proposed first implementation

Add a small `anomaly-detector` service to Docker Compose. It queries the Prometheus HTTP API on a fixed interval and evaluates a small, explicit set of rules. Keep the first release rule-based; the current demo has limited history and no labeled incident data to support a trustworthy ML model.

### Initial signals

1. **Target unavailable:** `up == 0` for a service across at least two scrapes.
2. **Elevated server error rate:** 5xx responses exceed a minimum request count and a configurable share of requests over a short window.
3. **Elevated latency:** the request duration p95 exceeds a configured threshold for a sustained window.
4. **Dependency/business failures:** order dependency errors or unavailable outcomes rise above a configured rate, with a minimum event count to avoid alerting on one request.

Use existing low-cardinality labels and route templates. Do not include user IDs, order IDs, transaction IDs, or raw request data in metrics or alert labels.

### Detector behavior

- Evaluate rules on a configurable interval, initially 10 seconds.
- Require a configurable duration or consecutive evaluation count before opening an anomaly, so one transient scrape does not alert.
- Assign each anomaly a stable identity based on rule and service; record first-seen time, last-seen time, severity, and a short explanation.
- Expose current anomalies and recent state transitions through a read-only API, and export detector health and anomaly counts as Prometheus metrics.
- Track `active`, `resolved`, and detector `unhealthy` states. Resolve an anomaly only after its signal remains normal for a configured recovery window.
- Keep active anomaly state in memory for the first demo iteration. A detector restart may clear active state; persistence is a later hardening decision.
- Treat Prometheus query failures separately from service anomalies, and expose detector health so missing monitoring data is visible.

## Delivery steps

### 1. Define and document rules

- Agree on initial thresholds, windows, minimum traffic, and severity for the three services.
- Add example PromQL queries and explain expected behavior for normal, failure, recovery, and no-traffic cases.
- Keep thresholds configurable through environment variables or a mounted configuration file.

### 2. Implement the detector service

- Add a bounded Prometheus API client with request timeouts and clear handling for missing or invalid query results.
- Add periodic rule evaluation, debounce/recovery windows, stable anomaly identities, and in-memory state transitions.
- Add `/health`, `/anomalies`, and `/metrics` endpoints.
- Do not expose an endpoint that can trigger service changes in this phase.

### 3. Integrate monitoring and dashboard

- Add the detector to `docker-compose.yml` and scrape its `/metrics` endpoint.
- Add Grafana panels for active anomalies, service availability, error-rate signals, and p95 latency.
- Show a clear state when the detector or Prometheus is unavailable, rather than presenting stale data as healthy.

### 4. Demonstrate and tune

- Demonstrate a clean baseline, injected 500/503 failures, slow requests, recovery, and Prometheus unavailability.
- Tune thresholds to avoid alerts from one-off errors and to detect sustained injected failures within a documented target window.
- Record known limitations, including no alerting during zero traffic for request-derived signals.

## Acceptance criteria

- Sustained injected service unavailability, server errors, and slow requests produce the expected anomaly with service, rule, severity, and timestamps.
- A single transient failure does not create an active anomaly under the configured debounce settings.
- An anomaly resolves only after its recovery window has elapsed.
- Zero traffic does not produce a false error-rate or latency anomaly; target availability remains independently detectable.
- Prometheus query failure makes detector health visibly unhealthy and is not misreported as a service recovery.
- Detector metrics are scraped by Prometheus, and Grafana shows active/resolved state clearly.
- The existing service APIs and failure controls continue to work without any automatic remediation.

## Out of scope for Phase 3

- Root-cause analysis or dependency graph reasoning.
- Machine-learning or adaptive thresholds.
- Automatic restart, rollback, traffic shifting, or other remediation.
- External paging or messaging integrations.
- Durable anomaly history across detector restarts.

## Suggested sequence after Phase 3

- **Phase 4: Root-cause analysis** — correlate anomalies across services and dependency calls, then produce a ranked explanation with evidence.
- **Phase 5: Guarded recovery** — add approved, bounded remediation actions with audit history, cooldowns, and a manual kill switch.

