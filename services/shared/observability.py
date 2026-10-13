"""Low-cardinality Prometheus metrics shared by the demo services."""
from __future__ import annotations

import time
from collections.abc import Callable

from fastapi import Request, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

HTTP_REQUESTS = Counter(
    "http_requests_total",
    "Completed HTTP requests.",
    ("service", "method", "route", "status_code"),
)
HTTP_REQUEST_DURATION = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration in seconds.",
    ("service", "method", "route"),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
HTTP_REQUESTS_IN_PROGRESS = Gauge(
    "http_requests_in_progress",
    "HTTP requests currently being handled.",
    ("service",),
)
BUSINESS_EVENTS = Counter(
    "business_events_total",
    "Business outcomes recorded by a service.",
    ("service", "event", "outcome"),
)


def record_business_event(service: str, event: str, outcome: str) -> None:
    BUSINESS_EVENTS.labels(service, event, outcome).inc()


def metrics_response() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


def instrument_requests(service: str) -> Callable:
    """Return FastAPI HTTP middleware that records route-template metrics."""

    async def middleware(request: Request, call_next):
        if request.url.path == "/metrics":
            return await call_next(request)

        started = time.perf_counter()
        status_code = 500
        HTTP_REQUESTS_IN_PROGRESS.labels(service).inc()
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            elapsed = time.perf_counter() - started
            route = request.scope.get("route")
            route_name = getattr(route, "path", "unmatched")
            HTTP_REQUESTS.labels(
                service, request.method, route_name, str(status_code)
            ).inc()
            HTTP_REQUEST_DURATION.labels(
                service, request.method, route_name
            ).observe(elapsed)
            HTTP_REQUESTS_IN_PROGRESS.labels(service).dec()

    return middleware
