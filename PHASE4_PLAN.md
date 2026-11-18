# Phase 4 Plan: Root-Cause Analysis

## Goal

For each active Phase 3 anomaly, explain the most likely affected component or dependency and show the evidence behind that explanation. Phase 4 produces ranked hypotheses for a human to review; it does not change service behavior or initiate recovery.

## Implementation status

Implemented in the current stack: order-service exports bounded dependency call totals and latency; the root-cause analyzer polls the anomaly detector and Prometheus, serves `/health` and `/diagnoses`, and exports health, ranked hypothesis, and numeric evidence metrics. Compose, Prometheus scraping, Grafana panels, and README usage guidance are wired in. The defaults are a two-minute evidence window, two dependency failures, at least two dependency calls, a 50% failure ratio, and a one-second dependency p95 latency threshold. Analyzer state remains in memory.

## Current baseline

- User, order, and payment services run in Docker Compose.
- Prometheus collects low-cardinality HTTP request, latency, in-progress, and business outcome metrics.
- The order service calls the user service and then the payment service using `httpx`.
- Phase 3's detector reports active and recently resolved service-level anomalies through `/anomalies`.
- The order service records aggregate dependency failures, but does not identify which dependency failed or expose dependency-call duration metrics.
- Metrics do not carry request, order, user, or transaction IDs. This is intentional to keep metric cardinality bounded.

## Scope and design

Implement deterministic, rule-based diagnosis for the current three-service topology. The initial analysis should use Phase 3 anomaly state plus dependency telemetry and service availability signals from Prometheus. Every hypothesis must include a rank, concise explanation, supporting evidence, and any important missing evidence. Use explicit confidence bands (`high`, `medium`, `low`), not a misleading numeric probability.

The diagnosis is an evidence-backed assessment, not proof of causality. Do not add an LLM or train an ML model in this phase; there is no labeled incident history to validate either approach.

## Phase 4 work

### 1. Instrument dependency calls

Add shared instrumentation around outgoing calls from order-service. Export bounded metrics such as:

- `dependency_requests_total{caller,dependency,operation,outcome}`
- `dependency_request_duration_seconds_bucket{caller,dependency,operation}`

Use only fixed service names, operation names, and a small outcome set such as `success`, `http_error`, `timeout`, and `connection_error`. Do not include URLs with identifiers, user IDs, order IDs, transaction IDs, exception text, or request payloads in labels. Ensure both HTTP error responses and transport exceptions are counted exactly once.

Keep the existing business outcome metrics. They describe order-level results, while the new metrics identify the dependency call associated with an outcome.

### 2. Define diagnosis rules and evidence

Start with transparent rules for these patterns:

- **Likely dependency outage:** dependency-call failures increase and the dependency's Prometheus target is unavailable.
- **Likely dependency degradation:** dependency-call failures or latency increase while the caller remains available.
- **Likely caller-side issue:** caller anomalies rise while dependency targets and dependency-call success/latency remain normal.
- **Multiple affected services:** several targets or dependency paths fail together; report a shared/uncertain cause rather than overclaiming one component.
- **Insufficient evidence:** an anomaly is present but traffic or metrics are missing to distinguish causes.

For each hypothesis, include the related anomaly IDs, the PromQL signal and evaluation window, observed values, configured threshold, and the time range. Separate directly observed facts from the inferred explanation.

### 3. Implement a read-only RCA API

Add a root-cause analyzer component that consumes active anomalies from the detector and queries Prometheus with bounded timeouts. Expose a read-only endpoint such as `/diagnoses` with:

- current analyzer health and last successful evaluation time;
- one diagnosis per active anomaly (or an explicit grouped diagnosis where evidence links anomalies);
- ranked hypotheses, confidence band, evidence, and missing-data notes;
- recent diagnosis changes with timestamps.

Keep analysis state in memory for the first iteration, matching the detector's current demo lifecycle. Treat detector or Prometheus query failures as analyzer-unhealthy; never turn missing data into a healthy diagnosis or a false resolution. Export analyzer health, query failures, and diagnosis counts as Prometheus metrics.

### 4. Integrate and display

Add the analyzer to Docker Compose and configure Prometheus to scrape its metrics. Add a Grafana view showing active anomalies next to the top diagnosis and evidence summary. The display must clearly distinguish an unknown/unavailable diagnosis from a diagnosis that found no likely cause.

Document local access, example responses, rule behavior, and limitations in the README.

## Delivery sequence

1. Add dependency telemetry and document metric names, label values, and cardinality limits.
2. Write diagnosis rules and example PromQL for healthy, user-service failure, payment-service failure, order-service failure, no-traffic, and monitoring-data-loss cases.
3. Implement the analyzer API, health behavior, bounded Prometheus client, and deterministic ranking.
4. Add Compose and Grafana integration.
5. Demonstrate each failure scenario and tune rules against clean baseline and recovery.

## Acceptance criteria

- Injected user-service or payment-service unavailability leads to a diagnosis that identifies that dependency as the leading hypothesis when dependency-call failures and target health support it.
- Injected order-service failure is not incorrectly blamed on a healthy downstream service; when the available metrics cannot localize it, the response says evidence is insufficient.
- Slow dependency responses can be distinguished from outright dependency unavailability using the dependency latency and outcome signals.
- Each diagnosis includes its evidence, thresholds/window, related anomaly, confidence band, and missing-data notes.
- No-traffic and absent metrics produce an unknown or insufficient-evidence result, not a healthy diagnosis.
- Detector or Prometheus unavailability makes analyzer health visibly unhealthy and is not presented as a root cause.
- Metrics remain bounded and contain no per-request or customer identifiers.
- Grafana displays anomaly and diagnosis state, including unavailable/unknown states.
- No service restart, traffic change, rollback, or other remediation is triggered by the analyzer.

## Out of scope

- Automated remediation or approval workflows (planned for Phase 5).
- LLM-based diagnosis, learned models, and numeric probability estimates.
- Distributed tracing rollout or cross-request event storage.
- Durable incident history, external alert delivery, or a general-purpose topology editor.
- Diagnosis beyond the currently modeled user → order → payment call graph.

## Risks and limitations

- Aggregate metrics can show temporal correlation but cannot prove that a particular dependency caused a particular request failure.
- Low traffic, scrape gaps, and the detector's in-memory state can reduce or erase diagnostic context.
- Existing order logic performs sequential user and payment calls; instrumentation must preserve request behavior while recording each outcome once.
- Fixed thresholds may need tuning for the demo workload; every threshold should remain configurable and documented.

## Exit criteria for Phase 5

Phase 4 is ready to inform guarded recovery when the failure scenarios above produce stable, inspectable evidence and the known uncertainty cases are explicit. Phase 5 can then map a small allowlisted set of high-confidence diagnoses to bounded, auditable recovery actions with cooldowns and a manual kill switch.
