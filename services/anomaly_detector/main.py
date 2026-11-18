from __future__ import annotations

import asyncio
import logging
import os
from collections import deque
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from prometheus_client import Counter, Gauge
from services.shared.observability import metrics_response

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://localhost:9090").rstrip("/")
EVALUATION_INTERVAL = max(1, int(os.getenv("EVALUATION_INTERVAL_SECONDS", "10")))
PROMETHEUS_TIMEOUT = max(1, float(os.getenv("PROMETHEUS_TIMEOUT_SECONDS", "3")))
ALERT_FOR_SECONDS = max(0, int(os.getenv("ALERT_FOR_SECONDS", "20")))
RECOVERY_FOR_SECONDS = max(0, int(os.getenv("RECOVERY_FOR_SECONDS", "30")))
ERROR_RATIO_THRESHOLD = min(1.0, max(0.0, float(os.getenv("ERROR_RATIO_THRESHOLD", "0.25"))))
ERROR_MIN_REQUESTS = max(1, int(os.getenv("ERROR_MIN_REQUESTS", "3")))
LATENCY_P95_THRESHOLD_SECONDS = max(
    0.01, float(os.getenv("LATENCY_P95_THRESHOLD_SECONDS", "1.0"))
)
DEPENDENCY_ERROR_MIN_EVENTS = max(1, int(os.getenv("DEPENDENCY_ERROR_MIN_EVENTS", "2")))
SERVICES = ("user-service", "order-service", "payment-service")
WINDOW = "2m"

ANOMALIES_ACTIVE = Gauge(
    "detector_anomalies_active",
    "Whether an anomaly is currently active for a rule and service.",
    ("rule", "service", "severity"),
)
DETECTOR_HEALTHY = Gauge(
    "detector_healthy", "Whether the detector can query Prometheus."
)
PROMETHEUS_QUERY_ERRORS = Counter(
    "detector_prometheus_query_errors_total",
    "Failed requests or responses from the Prometheus API.",
)

RULES = {
    "target_unavailable": "Prometheus cannot scrape this service target.",
    "elevated_error_rate": "The service 5xx response rate is elevated.",
    "elevated_latency": "The service p95 request latency is elevated.",
    "dependency_failures": "Order dependency failures are elevated.",
}
SEVERITY = {
    "target_unavailable": "critical",
    "elevated_error_rate": "warning",
    "elevated_latency": "warning",
    "dependency_failures": "warning",
}


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


class Detector:
    def __init__(self) -> None:
        self.client = httpx.AsyncClient(timeout=PROMETHEUS_TIMEOUT)
        self.states: dict[str, dict[str, Any]] = {}
        self.candidates: dict[str, datetime] = {}
        self.recoveries: dict[str, datetime] = {}
        self.transitions: deque[dict[str, Any]] = deque(maxlen=100)
        self.healthy = False
        for service in SERVICES:
            for rule in RULES:
                ANOMALIES_ACTIVE.labels(rule, service, SEVERITY[rule]).set(0)

    async def close(self) -> None:
        await self.client.aclose()

    async def query(self, expression: str) -> list[dict[str, Any]]:
        try:
            response = await self.client.get(
                f"{PROMETHEUS_URL}/api/v1/query", params={"query": expression}
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("status") != "success":
                raise ValueError("Prometheus returned a non-success query status")
            return payload["data"].get("result", [])
        except (httpx.HTTPError, ValueError, KeyError) as error:
            PROMETHEUS_QUERY_ERRORS.inc()
            raise RuntimeError(f"Prometheus query failed: {error}") from error

    @staticmethod
    def numeric_results(results: list[dict[str, Any]], label: str) -> dict[str, float]:
        values: dict[str, float] = {}
        for result in results:
            metric = result.get("metric", {})
            name = metric.get(label)
            sample = result.get("value", [None, None])
            try:
                if name is not None:
                    values[name] = float(sample[1])
            except (TypeError, ValueError, IndexError):
                continue
        return values

    async def evaluate(self) -> None:
        services_re = "|".join(SERVICES)
        checks: dict[str, dict[str, tuple[bool, str]]] = {
            service: {} for service in SERVICES
        }

        up = self.numeric_results(
            await self.query(f'up{{job=~"{services_re}"}}'), "job"
        )
        errors = self.numeric_results(
            await self.query(
                f'sum by (service) (increase(http_requests_total{{service=~"{services_re}",status_code=~"5.."}}[{WINDOW}]))'
            ),
            "service",
        )
        totals = self.numeric_results(
            await self.query(
                f'sum by (service) (increase(http_requests_total{{service=~"{services_re}"}}[{WINDOW}]))'
            ),
            "service",
        )
        latencies = self.numeric_results(
            await self.query(
                f'histogram_quantile(0.95, sum by (le, service) (rate(http_request_duration_seconds_bucket{{service=~"{services_re}"}}[{WINDOW}])))'
            ),
            "service",
        )
        dependencies = self.numeric_results(
            await self.query(
                f'sum by (service) (increase(business_events_total{{service="order-service",event="order_creation",outcome=~"dependency_error|dependency_unavailable"}}[{WINDOW}]))'
            ),
            "service",
        )

        for service in SERVICES:
            checks[service]["target_unavailable"] = (
                up.get(service, 0.0) == 0,
                "Prometheus target is missing or `up` is 0.",
            )
            total = totals.get(service, 0.0)
            error_count = errors.get(service, 0.0)
            error_ratio = error_count / total if total > 0 else 0.0
            checks[service]["elevated_error_rate"] = (
                total >= ERROR_MIN_REQUESTS and error_ratio >= ERROR_RATIO_THRESHOLD,
                f"5xx={error_count:.1f}, requests={total:.1f}, ratio={error_ratio:.0%}; threshold={ERROR_RATIO_THRESHOLD:.0%} with at least {ERROR_MIN_REQUESTS} requests in {WINDOW}.",
            )
            latency = latencies.get(service, 0.0)
            checks[service]["elevated_latency"] = (
                latency >= LATENCY_P95_THRESHOLD_SECONDS,
                f"p95={latency:.3f}s; threshold={LATENCY_P95_THRESHOLD_SECONDS:.3f}s over {WINDOW}.",
            )

        dependency_count = dependencies.get("order-service", 0.0)
        checks["order-service"]["dependency_failures"] = (
            dependency_count >= DEPENDENCY_ERROR_MIN_EVENTS,
            f"dependency failures={dependency_count:.1f}; threshold={DEPENDENCY_ERROR_MIN_EVENTS} in {WINDOW}.",
        )

        self.update_states(checks)

    def update_states(self, checks: dict[str, dict[str, tuple[bool, str]]]) -> None:
        timestamp = now()
        for service, service_checks in checks.items():
            for rule, (triggered, explanation) in service_checks.items():
                key = f"{service}:{rule}"
                state = self.states.get(key)
                if triggered:
                    self.recoveries.pop(key, None)
                    self.candidates.setdefault(key, timestamp)
                    if state and state["status"] == "active":
                        state["last_seen"] = iso(timestamp)
                        state["explanation"] = explanation
                    elif (timestamp - self.candidates[key]).total_seconds() >= ALERT_FOR_SECONDS:
                        anomaly = {
                            "id": key,
                            "rule": rule,
                            "service": service,
                            "severity": SEVERITY[rule],
                            "status": "active",
                            "first_seen": iso(self.candidates[key]),
                            "last_seen": iso(timestamp),
                            "resolved_at": None,
                            "explanation": explanation,
                        }
                        self.states[key] = anomaly
                        self.transitions.append({**anomaly, "transitioned_at": iso(timestamp)})
                        ANOMALIES_ACTIVE.labels(rule, service, SEVERITY[rule]).set(1)
                        logger.warning("Anomaly opened: %s", key)
                else:
                    self.candidates.pop(key, None)
                    if state and state["status"] == "active":
                        self.recoveries.setdefault(key, timestamp)
                        if (timestamp - self.recoveries[key]).total_seconds() >= RECOVERY_FOR_SECONDS:
                            state["status"] = "resolved"
                            state["resolved_at"] = iso(timestamp)
                            state["last_seen"] = iso(timestamp)
                            self.transitions.append({**state, "transitioned_at": iso(timestamp)})
                            ANOMALIES_ACTIVE.labels(rule, service, SEVERITY[rule]).set(0)
                            self.recoveries.pop(key, None)
                            logger.info("Anomaly resolved: %s", key)

    def active(self) -> list[dict[str, Any]]:
        return [state.copy() for state in self.states.values() if state["status"] == "active"]


detector = Detector()


async def evaluation_loop() -> None:
    while True:
        try:
            await detector.evaluate()
            detector.healthy = True
            DETECTOR_HEALTHY.set(1)
        except Exception:
            detector.healthy = False
            DETECTOR_HEALTHY.set(0)
            logger.exception("Anomaly evaluation failed")
        await asyncio.sleep(EVALUATION_INTERVAL)


@asynccontextmanager
async def lifespan(_: FastAPI):
    task = asyncio.create_task(evaluation_loop())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        await detector.close()


app = FastAPI(title="Anomaly Detector", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> JSONResponse:
    healthy = detector.healthy
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={
            "service": "anomaly-detector",
            "status": "healthy" if healthy else "unhealthy",
            "prometheus_url": PROMETHEUS_URL,
        },
    )


@app.get("/anomalies")
async def anomalies() -> dict[str, Any]:
    return {
        "detector_status": "healthy" if detector.healthy else "unhealthy",
        "active": detector.active(),
        "recent_transitions": list(detector.transitions),
    }


@app.get("/metrics", include_in_schema=False)
async def metrics():
    return metrics_response()

