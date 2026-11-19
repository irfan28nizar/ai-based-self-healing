"""
Phase 5 — Recovery controller tests.

All 10 tests are deterministic: no live network calls are made.
I/O methods (read_diagnoses, call_action, query_prometheus) are monkeypatched
so tests run in milliseconds without Docker or Prometheus running.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from services.recovery_controller import main


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------
def diagnosed_payment(confidence: str = "high") -> dict:
    """A fully diagnosed order-service with payment-service as the leading cause."""
    return {
        "service": "order-service",
        "status": "diagnosed",
        "related_anomaly_ids": ["order-service:dependency_failures"],
        "updated_at": "2026-11-19T00:00:00Z",
        "hypotheses": [
            {
                "rank": 1,
                "cause": "payment-service is unavailable",
                "confidence": confidence,
                "explanation": "Order calls to payment-service are failing.",
                "evidence": [],
            }
        ],
        "missing_evidence": [],
    }


def insufficient_evidence_diagnosis() -> dict:
    return {
        "service": "order-service",
        "status": "insufficient_evidence",
        "related_anomaly_ids": ["order-service:dependency_failures"],
        "updated_at": "2026-11-19T00:00:00Z",
        "hypotheses": [
            {
                "rank": 1,
                "cause": "insufficient evidence",
                "confidence": "low",
                "explanation": "Not enough data.",
                "evidence": [],
            }
        ],
        "missing_evidence": ["Insufficient traffic in window."],
    }


def _pending_proposal(action_id: str = "clear_payment_failure") -> dict:
    """A minimal pending proposal for use in duplicate/cooldown tests."""
    return {
        "proposal_id": "existing-id",
        "action_id": action_id,
        "service": "order-service",
        "target_service": "payment-service",
        "status": "pending",
        "dry_run": False,
        "diagnosis_ids": [],
        "confidence": "high",
        "cause": "payment-service is unavailable",
        "created_at": main.iso(main.now()),
        "expires_at": main.iso(main.now() + timedelta(seconds=120)),
        "approved_at": None,
        "started_at": None,
        "completed_at": None,
        "result": None,
        "verification_evidence": None,
    }


# ---------------------------------------------------------------------------
# Test 1 — insufficient_evidence diagnosis → proposal denied
# ---------------------------------------------------------------------------
def test_policy_denies_insufficient_evidence():
    controller = main.Controller()
    controller.healthy = True

    allowed, reason, action_id = controller._check_policy(insufficient_evidence_diagnosis())

    assert not allowed
    assert "insufficient_evidence" in reason
    assert action_id is None
    asyncio.run(controller.close())


# ---------------------------------------------------------------------------
# Test 2 — medium-confidence diagnosis denied when allowlist is "high" only
# ---------------------------------------------------------------------------
def test_policy_denies_low_confidence(monkeypatch):
    monkeypatch.setattr(main, "ALLOWED_CONFIDENCE", {"high"})
    controller = main.Controller()
    controller.healthy = True

    allowed, reason, action_id = controller._check_policy(diagnosed_payment(confidence="medium"))

    assert not allowed
    assert "confidence_medium_not_in_allowlist" in reason
    assert action_id is None
    asyncio.run(controller.close())


# ---------------------------------------------------------------------------
# Test 3 — kill switch denies at the very first gate
# ---------------------------------------------------------------------------
def test_policy_denies_when_kill_switch_active(monkeypatch):
    monkeypatch.setattr(main, "KILL_SWITCH", True)
    controller = main.Controller()
    controller.healthy = True  # inputs healthy — still denied by kill switch

    allowed, reason, action_id = controller._check_policy(diagnosed_payment())

    assert not allowed
    assert reason == "kill_switch_active"
    assert action_id is None
    asyncio.run(controller.close())


# ---------------------------------------------------------------------------
# Test 4 — a second proposal for the same action is blocked while one is active
# ---------------------------------------------------------------------------
def test_policy_denies_duplicate_proposal():
    controller = main.Controller()
    controller.healthy = True
    controller.proposals["existing-id"] = _pending_proposal("clear_payment_failure")

    allowed, reason, action_id = controller._check_policy(diagnosed_payment())

    assert not allowed
    assert reason == "duplicate_active_proposal"
    asyncio.run(controller.close())


# ---------------------------------------------------------------------------
# Test 5 — cooldown window blocks a repeated action for the same service
# ---------------------------------------------------------------------------
def test_policy_respects_cooldown(monkeypatch):
    monkeypatch.setattr(main, "COOLDOWN_SECONDS", 300)
    controller = main.Controller()
    controller.healthy = True
    # Simulate a recently completed action — elapsed = ~0s, well within 300s cooldown.
    controller.cooldowns["clear_payment_failure"] = main.now()

    allowed, reason, action_id = controller._check_policy(diagnosed_payment())

    assert not allowed
    assert "cooldown" in reason
    asyncio.run(controller.close())


# ---------------------------------------------------------------------------
# Test 6 — dry-run creates a proposal with status "dry_run" but never calls action
# ---------------------------------------------------------------------------
def test_dry_run_creates_proposal_but_does_not_execute(monkeypatch):
    monkeypatch.setattr(main, "DRY_RUN", True)
    calls: list = []

    controller = main.Controller()
    controller.healthy = True

    async def track_call(*_args, **_kwargs):
        calls.append(_args)
        return True

    monkeypatch.setattr(controller, "call_action", track_call)

    allowed, reason, action_id = controller._check_policy(diagnosed_payment())
    assert allowed, f"Policy unexpectedly denied: {reason}"

    proposal = controller._create_proposal(diagnosed_payment(), action_id)

    assert proposal["status"] == "dry_run"
    assert proposal["dry_run"] is True
    assert len(calls) == 0  # No HTTP call to the service was made
    asyncio.run(controller.close())


# ---------------------------------------------------------------------------
# Test 7 — an expired proposal cannot be approved
# ---------------------------------------------------------------------------
def test_expired_proposal_cannot_be_approved(monkeypatch):
    # Freeze "now" 200 seconds in the future so the 5-second expiry has passed.
    real_now = main.now()
    future_now = real_now + timedelta(seconds=200)
    monkeypatch.setattr(main, "now", lambda: future_now)

    controller = main.Controller()
    proposal_id = "test-expire"
    controller.proposals[proposal_id] = {
        "proposal_id": proposal_id,
        "service": "order-service",
        "action_id": "clear_payment_failure",
        "target_service": "payment-service",
        "diagnosis_ids": [],
        "confidence": "high",
        "cause": "payment-service is unavailable",
        "status": "pending",
        "dry_run": False,
        "created_at": main.iso(real_now),
        "expires_at": main.iso(real_now + timedelta(seconds=5)),  # 5s after creation; already past
        "approved_at": None,
        "started_at": None,
        "completed_at": None,
        "result": None,
        "verification_evidence": None,
    }

    with pytest.raises(ValueError, match="proposal_expired"):
        controller.approve(proposal_id)

    assert controller.proposals[proposal_id]["status"] == "expired"
    asyncio.run(controller.close())


# ---------------------------------------------------------------------------
# Test 8 — happy path: proposal → approve → execute → succeeded
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_approved_proposal_executes_allowlisted_action(monkeypatch):
    monkeypatch.setattr(main, "DRY_RUN", False)
    monkeypatch.setattr(main, "ALLOWED_CONFIDENCE", {"high"})
    monkeypatch.setattr(main, "VERIFY_WINDOW_SECONDS", 2)
    monkeypatch.setattr(main, "VERIFY_POLL_SECONDS", 0)

    controller = main.Controller()
    controller.healthy = True

    async def fake_call_action(*_args, **_kwargs):
        return True

    async def fake_query(*_args, **_kwargs):
        return 1.0  # postcondition satisfied on first poll

    monkeypatch.setattr(controller, "call_action", fake_call_action)
    monkeypatch.setattr(controller, "query_prometheus", fake_query)

    try:
        diagnosis = diagnosed_payment()
        allowed, reason, action_id = controller._check_policy(diagnosis)
        assert allowed, f"Policy denied: {reason}"

        proposal = controller._create_proposal(diagnosis, action_id)
        proposal_id = proposal["proposal_id"]

        # Manually put the proposal into "approved" state (simulates approve endpoint).
        proposal["status"] = "approved"
        proposal["approved_at"] = main.iso(main.now())

        await controller.execute(proposal_id)

        p = controller.proposals[proposal_id]
        assert p["status"] == "succeeded"
        assert p["result"] == "succeeded"
        assert p["verification_evidence"]["observed"] == 1.0
        # Cooldown was set after the action completed.
        assert "clear_payment_failure" in controller.cooldowns
        # Audit trail must include key lifecycle events.
        events = {r["event"] for r in controller.audit}
        assert "created" in events
        assert "executing" in events
        assert "succeeded" in events
    finally:
        await controller.close()


# ---------------------------------------------------------------------------
# Test 9 — unhealthy analyzer response blocks all proposal creation
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_unhealthy_analyzer_blocks_proposal(monkeypatch):
    controller = main.Controller()

    async def fake_read():
        # Analyzer reachable but reports itself unhealthy.
        return {"analyzer_status": "unhealthy", "active_diagnoses": []}

    monkeypatch.setattr(controller, "read_diagnoses", fake_read)

    try:
        with pytest.raises(RuntimeError, match="Analyzer reports unhealthy"):
            await controller.evaluate()
        assert not controller.healthy
        assert len(controller.proposals) == 0
    finally:
        await controller.close()


# ---------------------------------------------------------------------------
# Test 10 — verification reports "unknown" when postcondition is never met
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_verification_reports_unknown_on_postcondition_timeout(monkeypatch):
    monkeypatch.setattr(main, "DRY_RUN", False)
    # Zero-second window: the while-loop condition is false before the first iteration.
    monkeypatch.setattr(main, "VERIFY_WINDOW_SECONDS", 0)
    monkeypatch.setattr(main, "VERIFY_POLL_SECONDS", 0)

    controller = main.Controller()
    controller.healthy = True

    async def fake_call_action(*_args, **_kwargs):
        return True

    async def fake_query(*_args, **_kwargs):
        return 0.0  # postcondition NOT met

    monkeypatch.setattr(controller, "call_action", fake_call_action)
    monkeypatch.setattr(controller, "query_prometheus", fake_query)

    try:
        diagnosis = diagnosed_payment()
        allowed, reason, action_id = controller._check_policy(diagnosis)
        assert allowed

        proposal = controller._create_proposal(diagnosis, action_id)
        proposal_id = proposal["proposal_id"]
        proposal["status"] = "approved"
        proposal["approved_at"] = main.iso(main.now())

        await controller.execute(proposal_id)

        p = controller.proposals[proposal_id]
        assert p["status"] == "unknown"
        assert p["result"] == "unknown"
        # Audit must record the unknown outcome.
        events = {r["event"] for r in controller.audit}
        assert "unknown" in events
    finally:
        await controller.close()
