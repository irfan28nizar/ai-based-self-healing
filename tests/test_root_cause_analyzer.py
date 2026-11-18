import httpx
import pytest

from services.root_cause_analyzer import main
from services.shared.observability import DEPENDENCY_REQUESTS, request_dependency


def anomaly(service="order-service", rule="dependency_failures"):
    return {"id": f"{service}:{rule}", "service": service, "rule": rule}


def healthy_signals(**overrides):
    signals = {
        "up": {
            "user-service": 1.0,
            "order-service": 1.0,
            "payment-service": 1.0,
        },
        "dependency_totals": {"user-service": 10.0, "payment-service": 10.0},
        "dependency_failures": {},
        "dependency_p95": {"user-service": 0.05, "payment-service": 0.05},
    }
    signals.update(overrides)
    return signals


def test_payment_dependency_failure_is_ranked_with_evidence():
    analyzer = main.Analyzer()
    try:
        signals = healthy_signals(
            dependency_failures={"payment-service": 8.0},
            dependency_p95={"user-service": 0.05, "payment-service": 1.4},
        )
        diagnosis = analyzer.make_diagnosis("order-service", [anomaly()], signals)

        leading = diagnosis["hypotheses"][0]
        assert diagnosis["status"] == "diagnosed"
        assert leading["cause"] == "payment-service is failing or degraded"
        assert leading["rank"] == 1
        assert leading["confidence"] == "medium"
        assert {item["signal"] for item in leading["evidence"]} == {
            "dependency failure ratio",
            "failed dependency calls",
            "dependency p95 latency",
            "dependency target availability",
        }
        assert all(item["promql"] and item["window"] == main.WINDOW for item in leading["evidence"])
        ratio = next(item for item in leading["evidence"] if item["signal"] == "dependency failure ratio")
        assert ratio["observed"] == 0.8
        assert ratio["dependency"] == "payment-service"
    finally:
        import asyncio

        asyncio.run(analyzer.close())


def test_down_dependency_with_failed_calls_is_high_confidence():
    analyzer = main.Analyzer()
    try:
        signals = healthy_signals(
            up={"user-service": 1.0, "order-service": 1.0, "payment-service": 0.0},
            dependency_failures={"payment-service": 10.0},
        )
        diagnosis = analyzer.make_diagnosis("order-service", [anomaly()], signals)
        leading = diagnosis["hypotheses"][0]

        assert leading["cause"] == "payment-service is unavailable"
        assert leading["confidence"] == "high"
        assert any(item["signal"] == "dependency target availability" for item in leading["evidence"])
    finally:
        import asyncio

        asyncio.run(analyzer.close())


def test_no_dependency_traffic_returns_insufficient_evidence():
    analyzer = main.Analyzer()
    try:
        signals = healthy_signals(dependency_totals={}, dependency_failures={}, dependency_p95={})
        diagnosis = analyzer.make_diagnosis("order-service", [anomaly()], signals)

        assert diagnosis["status"] == "insufficient_evidence"
        assert diagnosis["hypotheses"][0]["cause"] == "insufficient evidence"
        assert any("Insufficient" in item for item in diagnosis["missing_evidence"])
    finally:
        import asyncio

        asyncio.run(analyzer.close())


def test_order_anomaly_without_dependency_traffic_is_not_localized():
    analyzer = main.Analyzer()
    try:
        signals = healthy_signals(dependency_totals={}, dependency_failures={}, dependency_p95={})
        diagnosis = analyzer.make_diagnosis(
            "order-service", [anomaly(rule="elevated_error_rate")], signals
        )

        assert diagnosis["status"] == "insufficient_evidence"
        assert diagnosis["hypotheses"][0]["cause"] == "insufficient evidence"
    finally:
        import asyncio

        asyncio.run(analyzer.close())


def test_simultaneous_target_failures_report_shared_uncertainty():
    analyzer = main.Analyzer()
    try:
        signals = healthy_signals(
            up={"user-service": 0.0, "order-service": 1.0, "payment-service": 0.0},
            dependency_failures={"user-service": 5.0, "payment-service": 5.0},
        )
        diagnosis = analyzer.make_diagnosis("order-service", [anomaly()], signals)

        assert diagnosis["hypotheses"][0]["cause"] == "multiple service targets unavailable"
        assert "shared cause" in diagnosis["hypotheses"][0]["explanation"]
        assert not any(item["cause"].endswith(" is unavailable") for item in diagnosis["hypotheses"])
        assert any("cannot determine whether" in item for item in diagnosis["missing_evidence"])
    finally:
        import asyncio

        asyncio.run(analyzer.close())


@pytest.mark.asyncio
async def test_analyzer_evaluation_records_diagnosis_change(monkeypatch):
    analyzer = main.Analyzer()
    monkeypatch.setattr(analyzer, "read_anomalies", lambda: _return([anomaly()]))
    monkeypatch.setattr(analyzer, "collect_signals", lambda: _return(healthy_signals()))
    try:
        await analyzer.evaluate()
        assert len(analyzer.diagnoses) == 1
        assert analyzer.last_successful_evaluation is not None
        assert len(analyzer.transitions) == 1
        assert analyzer.diagnoses[0]["status"] == "insufficient_evidence"
        await analyzer.evaluate()
        assert len(analyzer.transitions) == 1
    finally:
        await analyzer.close()


async def _return(value):
    return value


@pytest.mark.asyncio
async def test_dependency_request_records_expected_404_as_success():
    async def respond(_request):
        return httpx.Response(404, json={"detail": "User not found"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    label = DEPENDENCY_REQUESTS.labels("order-service", "user-service", "get_user", "success")
    before = label._value.get()
    try:
        response = await request_dependency(
            client,
            "GET",
            "http://user-service/users/missing-user",
            dependency="user-service",
            operation="get_user",
            expected_statuses=(404,),
        )
        assert response.status_code == 404
        assert label._value.get() == before + 1
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_dependency_request_records_http_error_and_timeout():
    async def respond(_request):
        return httpx.Response(503)

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    http_error = DEPENDENCY_REQUESTS.labels(
        "order-service", "payment-service", "create_payment", "http_error"
    )
    before_error = http_error._value.get()
    try:
        response = await request_dependency(
            client,
            "POST",
            "http://payment-service/payments",
            dependency="payment-service",
            operation="create_payment",
        )
        assert response.status_code == 503
        assert http_error._value.get() == before_error + 1
    finally:
        await client.aclose()

    async def timeout(_request):
        raise httpx.ReadTimeout("request timed out")

    client = httpx.AsyncClient(transport=httpx.MockTransport(timeout))
    timeout_counter = DEPENDENCY_REQUESTS.labels(
        "order-service", "payment-service", "create_payment", "timeout"
    )
    before_timeout = timeout_counter._value.get()
    try:
        with pytest.raises(httpx.ReadTimeout):
            await request_dependency(
                client,
                "POST",
                "http://payment-service/payments",
                dependency="payment-service",
                operation="create_payment",
            )
        assert timeout_counter._value.get() == before_timeout + 1
    finally:
        await client.aclose()
