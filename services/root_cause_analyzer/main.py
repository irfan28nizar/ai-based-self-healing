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
DETECTOR_URL = os.getenv("DETECTOR_URL", "http://localhost:8004").rstrip("/")
EVALUATION_INTERVAL = max(1, int(os.getenv("RCA_EVALUATION_INTERVAL_SECONDS", "10")))
REQUEST_TIMEOUT = max(1, float(os.getenv("RCA_REQUEST_TIMEOUT_SECONDS", "3")))
WINDOW = os.getenv("RCA_WINDOW", "2m")
MIN_DEPENDENCY_FAILURES = max(1, int(os.getenv("RCA_MIN_DEPENDENCY_FAILURES", "2")))
MIN_DEPENDENCY_REQUESTS = max(1, int(os.getenv("RCA_MIN_DEPENDENCY_REQUESTS", "2")))
DEPENDENCY_FAILURE_RATIO = min(1.0, max(0.0, float(os.getenv("RCA_DEPENDENCY_FAILURE_RATIO", "0.5"))))
DEPENDENCY_P95_THRESHOLD = max(0.01, float(os.getenv("RCA_DEPENDENCY_P95_THRESHOLD_SECONDS", "1.0")))
DEPENDENCIES = ("user-service", "payment-service")
SERVICES = ("user-service", "order-service", "payment-service")

ANALYZER_HEALTHY = Gauge("rca_analyzer_healthy", "Whether the root-cause analyzer can read its inputs.")
ANALYZER_QUERY_ERRORS = Counter("rca_analyzer_query_errors_total", "Failed detector or Prometheus reads.")
DIAGNOSES_ACTIVE = Gauge("rca_diagnoses_active", "Number of service diagnoses currently reported.")
HYPOTHESES_ACTIVE = Gauge("rca_hypotheses_active", "Number of ranked root-cause hypotheses currently reported.")
HYPOTHESIS_ACTIVE = Gauge(
    "rca_hypothesis_active",
    "Whether a bounded root-cause hypothesis is currently active.",
    ("service", "cause", "confidence", "rank"),
)
EVIDENCE_OBSERVED = Gauge(
    "rca_evidence_observed",
    "Latest numeric value for a bounded root-cause evidence signal.",
    ("service", "dependency", "signal"),
)


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


class Analyzer:
    def __init__(self) -> None:
        self.client = httpx.AsyncClient(timeout=REQUEST_TIMEOUT)
        self.healthy = False
        self.last_successful_evaluation: str | None = None
        self.diagnoses: list[dict[str, Any]] = []
        self.transitions: deque[dict[str, Any]] = deque(maxlen=100)
        self._signatures: dict[str, str] = {}

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
            ANALYZER_QUERY_ERRORS.inc()
            raise RuntimeError(f"Prometheus query failed: {error}") from error

    async def read_anomalies(self) -> list[dict[str, Any]]:
        try:
            response = await self.client.get(f"{DETECTOR_URL}/anomalies")
            response.raise_for_status()
            payload = response.json()
            if payload.get("detector_status") != "healthy":
                ANALYZER_QUERY_ERRORS.inc()
                raise RuntimeError("Anomaly detector reports unhealthy")
            return payload.get("active", [])
        except (httpx.HTTPError, ValueError, KeyError) as error:
            ANALYZER_QUERY_ERRORS.inc()
            raise RuntimeError(f"Anomaly detector read failed: {error}") from error

    @staticmethod
    def numbers(results: list[dict[str, Any]], label: str) -> dict[str, float]:
        values: dict[str, float] = {}
        for result in results:
            metric = result.get("metric", {})
            sample = result.get("value", [None, None])
            try:
                name = metric.get(label)
                if name is not None:
                    values[name] = float(sample[1])
            except (TypeError, ValueError, IndexError):
                continue
        return values

    async def collect_signals(self) -> dict[str, Any]:
        ups, totals, failures, latencies = await asyncio.gather(
            self.query('up{job=~"user-service|order-service|payment-service"}'),
            self.query(
                f'sum by (dependency) (increase(dependency_requests_total{{caller="order-service"}}[{WINDOW}]))'
            ),
            self.query(
                f'sum by (dependency) (increase(dependency_requests_total{{caller="order-service",outcome=~"http_error|timeout|connection_error"}}[{WINDOW}]))'
            ),
            self.query(
                f'histogram_quantile(0.95, sum by (le, dependency) (rate(dependency_request_duration_seconds_bucket{{caller="order-service"}}[{WINDOW}])))'
            ),
        )
        return {
            "up": self.numbers(ups, "job"),
            "dependency_totals": self.numbers(totals, "dependency"),
            "dependency_failures": self.numbers(failures, "dependency"),
            "dependency_p95": self.numbers(latencies, "dependency"),
        }

    def evidence(
        self,
        signal: str,
        observed: Any,
        threshold: Any,
        query: str,
        dependency: str = "none",
        unit: str | None = None,
    ) -> dict[str, Any]:
        return {
            "signal": signal,
            "observed": observed,
            "threshold": threshold,
            "window": WINDOW,
            "promql": query,
            "observed_at": iso(now()),
            "dependency": dependency,
            "unit": unit,
        }

    def make_diagnosis(self, service: str, anomalies: list[dict[str, Any]], signals: dict[str, Any]) -> dict[str, Any]:
        up = signals["up"]
        totals = signals["dependency_totals"]
        failures = signals["dependency_failures"]
        p95s = signals["dependency_p95"]
        hypotheses: list[dict[str, Any]] = []
        missing: list[str] = []
        observed_dependencies = 0
        for dependency in DEPENDENCIES:
            target = up.get(dependency)
            total = totals.get(dependency)
            failed = failures.get(dependency, 0.0) if total is not None else None
            latency = p95s.get(dependency)
            if target is None:
                missing.append(f"No Prometheus target health sample for {dependency}.")
            if total is None or total < MIN_DEPENDENCY_REQUESTS:
                missing.append(f"Insufficient {dependency} call traffic in {WINDOW} to assess its failure rate.")
                continue
            observed_dependencies += 1
            ratio = (failed or 0.0) / total
            down_evidence = target == 0 and (failed or 0.0) >= MIN_DEPENDENCY_FAILURES
            failure_evidence = (failed or 0.0) >= MIN_DEPENDENCY_FAILURES and ratio >= DEPENDENCY_FAILURE_RATIO
            slow_evidence = latency is not None and latency >= DEPENDENCY_P95_THRESHOLD
            if down_evidence:
                hypotheses.append({
                    "cause": f"{dependency} is unavailable",
                    "rank": 1,
                    "confidence": "high",
                    "explanation": f"Order calls to {dependency} are failing while Prometheus reports its target down.",
                    "evidence": [
                        self.evidence("dependency target availability", target, 1, f'up{{job="{dependency}"}}', dependency),
                        self.evidence("failed dependency calls", round(failed or 0, 2), f">={MIN_DEPENDENCY_FAILURES}", f'sum(increase(dependency_requests_total{{caller="order-service",dependency="{dependency}",outcome=~"http_error|timeout|connection_error"}}[{WINDOW}]))', dependency, "calls"),
                    ],
                })
            elif failure_evidence or slow_evidence:
                evidence = []
                if failure_evidence:
                    evidence.extend([
                        self.evidence("dependency failure ratio", round(ratio, 4), f">={DEPENDENCY_FAILURE_RATIO:.0%}", f'sum(increase(dependency_requests_total{{caller="order-service",dependency="{dependency}",outcome=~"http_error|timeout|connection_error"}}[{WINDOW}])) / sum(increase(dependency_requests_total{{caller="order-service",dependency="{dependency}"}}[{WINDOW}]))', dependency, "ratio"),
                        self.evidence("failed dependency calls", round(failed or 0, 2), f">={MIN_DEPENDENCY_FAILURES}", f'sum(increase(dependency_requests_total{{caller="order-service",dependency="{dependency}",outcome=~"http_error|timeout|connection_error"}}[{WINDOW}]))', dependency, "calls"),
                    ])
                if slow_evidence:
                    evidence.append(self.evidence("dependency p95 latency", round(latency, 4), f">={DEPENDENCY_P95_THRESHOLD:.3f}s", f'histogram_quantile(0.95, sum by (le) (rate(dependency_request_duration_seconds_bucket{{caller="order-service",dependency="{dependency}"}}[{WINDOW}])))', dependency, "seconds"))
                if target is not None:
                    evidence.append(self.evidence("dependency target availability", target, "1 means scrape reachable", f'up{{job="{dependency}"}}', dependency))
                hypotheses.append({
                    "cause": f"{dependency} is failing or degraded",
                    "rank": 1,
                    "confidence": "medium",
                    "explanation": f"Order-service telemetry shows elevated failures or latency on calls to {dependency}; this supports a dependency-side hypothesis but does not prove causality for individual requests.",
                    "evidence": evidence,
                })

        service_target = up.get(service)
        local_anomaly_types = {a.get("rule") for a in anomalies}
        if service_target == 0:
            hypotheses.append({
                "cause": f"{service} is unavailable",
                "rank": 1,
                "confidence": "high",
                "explanation": "Prometheus reports the affected service target as unavailable.",
                "evidence": [self.evidence("service target availability", service_target, 1, f'up{{job="{service}"}}')],
            })
        elif service == "order-service" and local_anomaly_types.intersection({"elevated_error_rate", "elevated_latency"}) and not hypotheses:
            downstream_known_healthy = (
                all(up.get(dep) == 1 for dep in DEPENDENCIES)
                and observed_dependencies == len(DEPENDENCIES)
            )
            hypotheses.append({
                "cause": "order-service local behavior" if downstream_known_healthy else "insufficient evidence",
                "rank": 1,
                "confidence": "low",
                "explanation": "Order service is the leading low-confidence hypothesis because both dependencies are reachable and have enough normal call traffic." if downstream_known_healthy else "The order service has an active anomaly, but there is not enough dependency traffic and health evidence to localize its cause.",
                "evidence": [self.evidence("active service anomalies", [a.get("id") for a in anomalies], "active anomaly", "detector /anomalies")],
            })

        if not hypotheses:
            hypotheses.append({
                "cause": "insufficient evidence",
                "rank": 1,
                "confidence": "low",
                "explanation": "An anomaly is active, but available traffic and health signals do not distinguish a likely cause.",
                "evidence": [self.evidence("active service anomalies", [a.get("id") for a in anomalies], "active anomaly", "detector /anomalies")],
            })
            if observed_dependencies == len(DEPENDENCIES) and not missing:
                missing.append("No dependency failure or latency signal crossed its configured threshold.")

        down_services = sorted(name for name, value in up.items() if value == 0)
        if len(down_services) > 1 and (service in down_services or service == "order-service"):
            hypotheses = [
                item for item in hypotheses
                if not item["cause"].endswith(" is unavailable")
            ]
            hypotheses.append({
                "cause": "multiple service targets unavailable",
                "rank": 1,
                "confidence": "medium",
                "explanation": "Several services are unreachable at once; the shared cause is uncertain and needs operator review.",
                "evidence": [
                    self.evidence("unavailable service targets", down_services, "one or more targets unavailable", 'up{job=~"user-service|order-service|payment-service"}'),
                ],
            })
            missing.append("The current metrics cannot determine whether these failures share one underlying cause.")

        hypotheses.sort(key=lambda item: (item["rank"], {"high": 0, "medium": 1, "low": 2}[item["confidence"]]))
        for index, hypothesis in enumerate(hypotheses, start=1):
            hypothesis["rank"] = index
        return {
            "service": service,
            "status": "diagnosed" if hypotheses[0]["cause"] != "insufficient evidence" else "insufficient_evidence",
            "related_anomaly_ids": [a.get("id") for a in anomalies],
            "updated_at": iso(now()),
            "hypotheses": hypotheses,
            "missing_evidence": sorted(set(missing)),
        }

    async def evaluate(self) -> None:
        anomalies, signals = await asyncio.gather(self.read_anomalies(), self.collect_signals())
        grouped: dict[str, list[dict[str, Any]]] = {}
        for anomaly in anomalies:
            service = anomaly.get("service")
            if service in SERVICES:
                grouped.setdefault(service, []).append(anomaly)
        diagnoses = [self.make_diagnosis(service, items, signals) for service, items in sorted(grouped.items())]
        current = {item["service"]: item for item in diagnoses}
        for service, diagnosis in current.items():
            signature = repr([(h["cause"], h["confidence"]) for h in diagnosis["hypotheses"]])
            if self._signatures.get(service) != signature:
                self.transitions.append({"service": service, "transitioned_at": iso(now()), "diagnosis": diagnosis})
            self._signatures[service] = signature
        for old_service in set(self._signatures) - set(current):
            self.transitions.append({"service": old_service, "transitioned_at": iso(now()), "status": "resolved"})
            self._signatures.pop(old_service, None)
        self.diagnoses = diagnoses
        self.last_successful_evaluation = iso(now())
        for metric in (HYPOTHESIS_ACTIVE, EVIDENCE_OBSERVED):
            for series in list(metric._metrics.values()):
                series.set(0)
        for diagnosis in diagnoses:
            service = diagnosis["service"]
            for hypothesis in diagnosis["hypotheses"]:
                HYPOTHESIS_ACTIVE.labels(
                    service,
                    hypothesis["cause"],
                    hypothesis["confidence"],
                    str(hypothesis["rank"]),
                ).set(1)
                for item in hypothesis["evidence"]:
                    observed = item.get("observed")
                    if isinstance(observed, (int, float)):
                        EVIDENCE_OBSERVED.labels(
                            service, item.get("dependency", "none"), item["signal"]
                        ).set(observed)
        DIAGNOSES_ACTIVE.set(len(diagnoses))
        HYPOTHESES_ACTIVE.set(sum(len(item["hypotheses"]) for item in diagnoses))

    def snapshot(self) -> dict[str, Any]:
        return {
            "analyzer_status": "healthy" if self.healthy else "unhealthy",
            "diagnoses_stale": not self.healthy,
            "last_successful_evaluation": self.last_successful_evaluation,
            "window": WINDOW,
            "active_diagnoses": self.diagnoses,
            "recent_changes": list(self.transitions),
        }


analyzer = Analyzer()


async def evaluation_loop() -> None:
    while True:
        try:
            await analyzer.evaluate()
            analyzer.healthy = True
            ANALYZER_HEALTHY.set(1)
        except Exception:
            analyzer.healthy = False
            ANALYZER_HEALTHY.set(0)
            logger.exception("Root-cause analysis evaluation failed")
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
        await analyzer.close()


app = FastAPI(title="Root-Cause Analyzer", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse(
        status_code=200 if analyzer.healthy else 503,
        content={"service": "root-cause-analyzer", **analyzer.snapshot()},
    )


@app.get("/diagnoses")
async def diagnoses() -> dict[str, Any]:
    return analyzer.snapshot()


@app.get("/metrics", include_in_schema=False)
async def metrics():
    return metrics_response()
