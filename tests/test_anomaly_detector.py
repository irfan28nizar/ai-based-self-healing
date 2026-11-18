from datetime import datetime, timedelta, timezone

import pytest

from services.anomaly_detector import main


def vector(value, **labels):
    return {"metric": labels, "value": [0, str(value)]}


@pytest.mark.asyncio
async def test_evaluate_opens_error_latency_and_dependency_anomalies(monkeypatch):
    detector = main.Detector()
    monkeypatch.setattr(main, "ALERT_FOR_SECONDS", 0)
    responses = iter(
        [
            [
                vector(1, job="user-service"),
                vector(1, job="order-service"),
                vector(1, job="payment-service"),
            ],
            [vector(3, service="order-service")],
            [
                vector(4, service="user-service"),
                vector(4, service="order-service"),
                vector(4, service="payment-service"),
            ],
            [
                vector(0.1, service="user-service"),
                vector(0.1, service="order-service"),
                vector(2.0, service="payment-service"),
            ],
            [vector(2, service="order-service")],
        ]
    )

    async def fake_query(_expression):
        return next(responses)

    monkeypatch.setattr(detector, "query", fake_query)
    try:
        await detector.evaluate()
        active = {item["id"] for item in detector.active()}
        assert active == {
            "order-service:elevated_error_rate",
            "payment-service:elevated_latency",
            "order-service:dependency_failures",
        }
    finally:
        await detector.close()


def test_anomaly_requires_sustained_signal_and_recovery(monkeypatch):
    detector = main.Detector()
    start = datetime(2026, 11, 5, tzinfo=timezone.utc)
    clock = {"now": start}
    monkeypatch.setattr(main, "now", lambda: clock["now"])
    monkeypatch.setattr(main, "ALERT_FOR_SECONDS", 20)
    monkeypatch.setattr(main, "RECOVERY_FOR_SECONDS", 30)
    checks = {"user-service": {"elevated_error_rate": (True, "high errors")}}

    try:
        detector.update_states(checks)
        clock["now"] += timedelta(seconds=10)
        detector.update_states(checks)
        assert detector.active() == []

        clock["now"] += timedelta(seconds=10)
        detector.update_states(checks)
        assert len(detector.active()) == 1
        assert detector.active()[0]["first_seen"] == start.isoformat().replace("+00:00", "Z")

        recovered = {"user-service": {"elevated_error_rate": (False, "normal")}}
        for _ in range(4):
            clock["now"] += timedelta(seconds=10)
            detector.update_states(recovered)
        assert detector.active() == []
        assert detector.states["user-service:elevated_error_rate"]["status"] == "resolved"
    finally:
        # Detector instances own an async HTTP client; close it without network traffic.
        import asyncio

        asyncio.run(detector.close())
