from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections import deque
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from prometheus_client import Counter, Gauge

from services.shared.observability import metrics_response

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration — all values are configurable via environment variables.
# ---------------------------------------------------------------------------
ANALYZER_URL = os.getenv("ANALYZER_URL", "http://localhost:8005").rstrip("/")
PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://localhost:9090").rstrip("/")
SERVICE_URLS: dict[str, str] = {
    "user-service": os.getenv("USER_SERVICE_URL", "http://localhost:8001").rstrip("/"),
    "payment-service": os.getenv("PAYMENT_SERVICE_URL", "http://localhost:8003").rstrip("/"),
    "order-service": os.getenv("ORDER_SERVICE_URL", "http://localhost:8002").rstrip("/"),
}

# Safety gates — both default to the safe/inactive state.
DRY_RUN = os.getenv("RC_DRY_RUN", "true").lower() not in ("false", "0", "no")
KILL_SWITCH = os.getenv("RC_KILL_SWITCH", "false").lower() in ("true", "1", "yes")

EVALUATION_INTERVAL = max(1, int(os.getenv("RC_EVALUATION_INTERVAL_SECONDS", "15")))
REQUEST_TIMEOUT = max(1, float(os.getenv("RC_REQUEST_TIMEOUT_SECONDS", "3")))
APPROVAL_EXPIRY_SECONDS = max(10, int(os.getenv("RC_APPROVAL_EXPIRY_SECONDS", "120")))
COOLDOWN_SECONDS = max(0, int(os.getenv("RC_COOLDOWN_SECONDS", "300")))
ALLOWED_CONFIDENCE: set[str] = set(os.getenv("RC_ALLOWED_CONFIDENCE", "high").split(","))
VERIFY_WINDOW_SECONDS = max(1, int(os.getenv("RC_VERIFY_WINDOW_SECONDS", "60")))
VERIFY_POLL_SECONDS = max(1, int(os.getenv("RC_VERIFY_POLL_SECONDS", "5")))

# ---------------------------------------------------------------------------
# Action registry — the ONLY actions the controller may ever execute.
#
# Each entry describes one allowlisted /admin/failure call and the Prometheus
# postcondition that confirms recovery. No Docker socket, no shell commands,
# no URLs with identifiers, no credentials.
# ---------------------------------------------------------------------------
ACTION_REGISTRY: dict[str, dict[str, Any]] = {
    "clear_payment_failure": {
        "action_id": "clear_payment_failure",
        "target_service": "payment-service",
        "endpoint": "/admin/failure",
        "payload": {"enabled": False},
        "postcondition_query": 'up{job="payment-service"}',
        "postcondition_target": 1.0,
    },
    "clear_user_failure": {
        "action_id": "clear_user_failure",
        "target_service": "user-service",
        "endpoint": "/admin/failure",
        "payload": {"enabled": False},
        "postcondition_query": 'up{job="user-service"}',
        "postcondition_target": 1.0,
    },
    "clear_order_failure": {
        "action_id": "clear_order_failure",
        "target_service": "order-service",
        "endpoint": "/admin/failure",
        "payload": {"enabled": False},
        "postcondition_query": 'up{job="order-service"}',
        "postcondition_target": 1.0,
    },
}

# Maps the leading service-name prefix of a hypothesis cause string to an action ID.
# Only services with allowlisted actions are present; all other causes are skipped.
CAUSE_ACTION_MAP: dict[str, str] = {
    "payment-service": "clear_payment_failure",
    "user-service": "clear_user_failure",
    "order-service": "clear_order_failure",
}

# ---------------------------------------------------------------------------
# Prometheus metrics — all bounded, no per-request or customer identifiers.
# ---------------------------------------------------------------------------
RC_CONTROLLER_HEALTHY = Gauge(
    "rc_controller_healthy",
    "1 if the recovery controller inputs are reachable and the analyzer is healthy.",
)
RC_KILL_SWITCH_ACTIVE = Gauge(
    "rc_kill_switch_active",
    "1 if the kill switch is currently active.",
)
RC_DRY_RUN_ACTIVE = Gauge(
    "rc_dry_run_active",
    "1 if the controller is in dry-run mode (proposes but does not execute).",
)
RC_PROPOSALS_CREATED = Counter(
    "rc_proposals_created_total",
    "Number of recovery proposals generated.",
    ("service", "action"),
)
RC_PROPOSALS_DENIED = Counter(
    "rc_proposals_denied_total",
    "Number of recovery proposals denied by the policy engine.",
    ("service", "reason"),
)
RC_ACTIONS = Counter(
    "rc_actions_total",
    "Number of completed recovery action attempts and their outcomes.",
    ("service", "action", "result"),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def new_id() -> str:
    return uuid.uuid4().hex[:16]


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------
class Controller:
    def __init__(self) -> None:
        self.client = httpx.AsyncClient(timeout=REQUEST_TIMEOUT)
        self.healthy = False
        self.last_successful_evaluation: str | None = None
        self.proposals: dict[str, dict[str, Any]] = {}
        self.audit: deque[dict[str, Any]] = deque(maxlen=200)
        # cooldowns maps action_id → datetime of last completed action.
        self.cooldowns: dict[str, datetime] = {}

    async def close(self) -> None:
        await self.client.aclose()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _record_audit(
        self,
        proposal_id: str,
        event: str,
        service: str,
        action_id: str,
        detail: str = "",
    ) -> None:
        """Append one structured audit record. Never logs secrets or payloads."""
        self.audit.append(
            {
                "record_id": new_id(),
                "proposal_id": proposal_id,
                "event": event,
                "service": service,
                "action_id": action_id,
                "timestamp": iso(now()),
                "detail": detail,
            }
        )

    def _cause_to_action(self, cause: str) -> str | None:
        """Return the action_id for the first matching service prefix in the cause."""
        for prefix, action_id in CAUSE_ACTION_MAP.items():
            if cause.startswith(prefix):
                return action_id
        return None

    # ------------------------------------------------------------------
    # I/O — all patchable in tests via monkeypatch
    # ------------------------------------------------------------------
    async def read_diagnoses(self) -> dict[str, Any]:
        try:
            response = await self.client.get(f"{ANALYZER_URL}/diagnoses")
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise RuntimeError(f"Analyzer read failed: {error}") from error

    async def query_prometheus(self, expression: str) -> float | None:
        """Instant query; returns the first scalar result or None on any failure."""
        try:
            response = await self.client.get(
                f"{PROMETHEUS_URL}/api/v1/query",
                params={"query": expression},
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("status") != "success":
                return None
            results = payload["data"].get("result", [])
            if not results:
                return None
            return float(results[0]["value"][1])
        except Exception:
            return None

    async def call_action(
        self,
        target_service: str,
        endpoint: str,
        payload: dict[str, Any],
    ) -> bool:
        """
        Make the allowlisted /admin/failure call.
        Returns True on HTTP 2xx, False on any error.
        No arbitrary commands, no Docker socket access.
        """
        base_url = SERVICE_URLS.get(target_service, "")
        if not base_url:
            logger.error("No URL configured for service %r — action blocked.", target_service)
            return False
        try:
            response = await self.client.post(f"{base_url}{endpoint}", json=payload)
            return response.is_success
        except (httpx.HTTPError, httpx.RequestError) as error:
            logger.warning("Action HTTP call failed for %s: %s", target_service, error)
            return False

    # ------------------------------------------------------------------
    # Policy engine — 7 ordered gates; all must pass to allow a proposal.
    # ------------------------------------------------------------------
    def _check_policy(
        self, diagnosis: dict[str, Any]
    ) -> tuple[bool, str, str | None]:
        """
        Run all 7 ordered policy gates.
        Returns (allowed, denial_reason, action_id).
        denial_reason is "" and action_id is set when allowed=True.
        """
        # Gate 1: Kill switch — stops all execution immediately.
        if KILL_SWITCH:
            return False, "kill_switch_active", None

        # Gate 2: Controller inputs must be healthy (analyzer reachable).
        if not self.healthy:
            return False, "controller_inputs_unhealthy", None

        # Gate 3: Diagnosis status must be "diagnosed", not "insufficient_evidence".
        if diagnosis.get("status") != "diagnosed":
            return False, f"status_{diagnosis.get('status', 'unknown')}", None

        # Gate 4: Leading hypothesis confidence must be in the configured allowlist.
        hypotheses = diagnosis.get("hypotheses", [])
        if not hypotheses:
            return False, "no_hypotheses", None
        leading = hypotheses[0]
        confidence = leading.get("confidence", "unknown")
        if confidence not in ALLOWED_CONFIDENCE:
            return False, f"confidence_{confidence}_not_in_allowlist", None

        # Gate 5: Cause must map to an allowlisted action.
        cause = leading.get("cause", "")
        action_id = self._cause_to_action(cause)
        if action_id is None:
            return False, "no_action_mapped_to_cause", None

        # Gate 6: No existing pending/approved/executing proposal for the same action.
        for proposal in self.proposals.values():
            if (
                proposal["action_id"] == action_id
                and proposal["status"] in ("pending", "approved", "executing")
            ):
                return False, "duplicate_active_proposal", None

        # Gate 7: Cooldown window must have fully elapsed since the last action.
        last_time = self.cooldowns.get(action_id)
        if last_time is not None:
            elapsed = (now() - last_time).total_seconds()
            if elapsed < COOLDOWN_SECONDS:
                remaining = int(COOLDOWN_SECONDS - elapsed)
                return False, f"cooldown_{remaining}s_remaining", None

        return True, "", action_id

    # ------------------------------------------------------------------
    # Proposal lifecycle
    # ------------------------------------------------------------------
    def _create_proposal(
        self, diagnosis: dict[str, Any], action_id: str
    ) -> dict[str, Any]:
        """
        Create and store a new proposal. Status is "dry_run" in dry-run mode,
        or "pending" in live mode. Expiry is set at creation time.
        """
        proposal_id = new_id()
        action_def = ACTION_REGISTRY[action_id]
        service = diagnosis.get("service", "unknown")
        leading = (diagnosis.get("hypotheses") or [{}])[0]
        proposal: dict[str, Any] = {
            "proposal_id": proposal_id,
            "service": service,
            "action_id": action_id,
            "target_service": action_def["target_service"],
            "diagnosis_ids": diagnosis.get("related_anomaly_ids", []),
            "confidence": leading.get("confidence"),
            "cause": leading.get("cause"),
            "status": "dry_run" if DRY_RUN else "pending",
            "dry_run": DRY_RUN,
            "created_at": iso(now()),
            "expires_at": iso(now() + timedelta(seconds=APPROVAL_EXPIRY_SECONDS)),
            "approved_at": None,
            "started_at": None,
            "completed_at": None,
            "result": None,
            "verification_evidence": None,
        }
        self.proposals[proposal_id] = proposal
        RC_PROPOSALS_CREATED.labels(service, action_id).inc()
        self._record_audit(
            proposal_id,
            "created",
            service,
            action_id,
            f"dry_run={DRY_RUN} cause={leading.get('cause')!r}",
        )
        logger.info(
            "Proposal %s created: action=%s service=%s dry_run=%s",
            proposal_id,
            action_id,
            service,
            DRY_RUN,
        )
        return proposal

    def approve(self, proposal_id: str) -> dict[str, Any]:
        """
        Approve a pending proposal.
        Raises KeyError if not found.
        Raises ValueError with reason if the approval cannot proceed.
        """
        proposal = self.proposals.get(proposal_id)
        if proposal is None:
            raise KeyError(proposal_id)
        if proposal["status"] not in ("pending",):
            raise ValueError(f"Cannot approve proposal with status {proposal['status']!r}")
        # Stale approvals are rejected; the expiry was set at proposal creation.
        expires = datetime.fromisoformat(proposal["expires_at"].replace("Z", "+00:00"))
        if now() > expires:
            proposal["status"] = "expired"
            proposal["completed_at"] = iso(now())
            self._record_audit(
                proposal_id,
                "expired",
                proposal["service"],
                proposal["action_id"],
                "approval attempted after proposal expiry",
            )
            raise ValueError("proposal_expired")
        proposal["status"] = "approved"
        proposal["approved_at"] = iso(now())
        self._record_audit(
            proposal_id,
            "approved",
            proposal["service"],
            proposal["action_id"],
        )
        logger.info("Proposal %s approved", proposal_id)
        return proposal

    # ------------------------------------------------------------------
    # Execution and verification
    # ------------------------------------------------------------------
    async def execute(self, proposal_id: str) -> None:
        """
        Execute an approved proposal. Called as an asyncio background task
        immediately after approval. Only allowlisted actions are ever called.
        """
        proposal = self.proposals.get(proposal_id)
        if proposal is None or proposal["status"] != "approved":
            return

        # Belt-and-suspenders: re-check kill switch at execution time.
        if KILL_SWITCH:
            proposal["status"] = "denied"
            proposal["result"] = "kill_switch_active_at_execution"
            proposal["completed_at"] = iso(now())
            self._record_audit(
                proposal_id,
                "denied_at_execution",
                proposal["service"],
                proposal["action_id"],
                "kill switch was active when execution was attempted",
            )
            logger.warning("Execution denied by kill switch: %s", proposal_id)
            return

        # DRY_RUN proposals are created with status "dry_run" and should never
        # reach execute(). This guard prevents accidental execution if mode changes.
        if DRY_RUN:
            logger.warning(
                "execute() called for proposal %s but DRY_RUN=True; skipping.",
                proposal_id,
            )
            return

        proposal["status"] = "executing"
        proposal["started_at"] = iso(now())
        self._record_audit(
            proposal_id,
            "executing",
            proposal["service"],
            proposal["action_id"],
        )

        action_def = ACTION_REGISTRY[proposal["action_id"]]
        success = await self.call_action(
            action_def["target_service"],
            action_def["endpoint"],
            action_def["payload"],
        )

        if not success:
            proposal["status"] = "failed"
            proposal["result"] = "action_http_call_failed"
            proposal["completed_at"] = iso(now())
            self.cooldowns[proposal["action_id"]] = now()
            RC_ACTIONS.labels(proposal["service"], proposal["action_id"], "failed").inc()
            self._record_audit(
                proposal_id,
                "failed",
                proposal["service"],
                proposal["action_id"],
                "allowlisted action HTTP call did not succeed",
            )
            logger.warning("Action failed: %s", proposal_id)
            return

        self._record_audit(
            proposal_id,
            "action_called",
            proposal["service"],
            proposal["action_id"],
            "HTTP call succeeded; starting verification window",
        )

        # Verification window: poll Prometheus until the postcondition is met
        # or the window expires. Never claim success on the HTTP call result alone.
        deadline = now() + timedelta(seconds=VERIFY_WINDOW_SECONDS)
        result = "unknown"
        last_value: float | None = None
        while now() < deadline:
            await asyncio.sleep(VERIFY_POLL_SECONDS)
            value = await self.query_prometheus(action_def["postcondition_query"])
            last_value = value
            if value is not None and value == action_def["postcondition_target"]:
                result = "succeeded"
                break

        proposal["status"] = result
        proposal["result"] = result
        proposal["completed_at"] = iso(now())
        proposal["verification_evidence"] = {
            "query": action_def["postcondition_query"],
            "target": action_def["postcondition_target"],
            "observed": last_value,
            "window_seconds": VERIFY_WINDOW_SECONDS,
        }
        self.cooldowns[proposal["action_id"]] = now()
        RC_ACTIONS.labels(proposal["service"], proposal["action_id"], result).inc()
        self._record_audit(
            proposal_id,
            result,
            proposal["service"],
            proposal["action_id"],
            f"postcondition observed={last_value} target={action_def['postcondition_target']}",
        )
        logger.info(
            "Action %s %s: proposal=%s",
            proposal["action_id"],
            result,
            proposal_id,
        )

    # ------------------------------------------------------------------
    # Evaluation loop body
    # ------------------------------------------------------------------
    async def evaluate(self) -> None:
        """
        Read diagnoses from the analyzer, run policy checks, and create proposals.
        Also expires any stale approved proposals that were never executed.
        Raises RuntimeError if inputs are unavailable or unhealthy.
        """
        payload = await self.read_diagnoses()
        if payload.get("analyzer_status") != "healthy":
            self.healthy = False
            raise RuntimeError("Analyzer reports unhealthy status")

        self.healthy = True
        self.last_successful_evaluation = iso(now())

        for diagnosis in payload.get("active_diagnoses", []):
            allowed, reason, action_id = self._check_policy(diagnosis)
            if allowed and action_id:
                self._create_proposal(diagnosis, action_id)
            else:
                service = diagnosis.get("service", "unknown")
                RC_PROPOSALS_DENIED.labels(service, reason).inc()
                logger.debug("Proposal denied for %s: %s", service, reason)

        # Expire stale approved proposals that were never executed in time.
        for proposal in self.proposals.values():
            if proposal["status"] == "approved" and proposal.get("expires_at"):
                expires = datetime.fromisoformat(
                    proposal["expires_at"].replace("Z", "+00:00")
                )
                if now() > expires:
                    proposal["status"] = "expired"
                    proposal["completed_at"] = iso(now())
                    self._record_audit(
                        proposal["proposal_id"],
                        "expired",
                        proposal["service"],
                        proposal["action_id"],
                        "proposal expiry elapsed before execution could start",
                    )

    def snapshot(self) -> dict[str, Any]:
        return {
            "controller_status": "healthy" if self.healthy else "unhealthy",
            "dry_run": DRY_RUN,
            "kill_switch": KILL_SWITCH,
            "last_successful_evaluation": self.last_successful_evaluation,
            "proposals": list(self.proposals.values()),
        }


# ---------------------------------------------------------------------------
# Module-level singleton and background evaluation loop
# ---------------------------------------------------------------------------
controller = Controller()


async def evaluation_loop() -> None:
    RC_DRY_RUN_ACTIVE.set(1 if DRY_RUN else 0)
    RC_KILL_SWITCH_ACTIVE.set(1 if KILL_SWITCH else 0)
    while True:
        try:
            await controller.evaluate()
            RC_CONTROLLER_HEALTHY.set(1)
        except Exception:
            controller.healthy = False
            RC_CONTROLLER_HEALTHY.set(0)
            logger.exception("Recovery controller evaluation failed")
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
        await controller.close()


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Recovery Controller",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse(
        status_code=200 if controller.healthy else 503,
        content={"service": "recovery-controller", **controller.snapshot()},
    )


@app.get("/proposals")
async def list_proposals() -> dict[str, Any]:
    return controller.snapshot()


@app.get("/proposals/{proposal_id}")
async def get_proposal(proposal_id: str) -> dict[str, Any]:
    proposal = controller.proposals.get(proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail="Proposal not found")
    return proposal


@app.post("/proposals/{proposal_id}/approve")
async def approve_proposal(proposal_id: str) -> dict[str, Any]:
    """
    Approve a pending proposal. Approval is bound to this specific proposal ID
    and the expiry set at creation time; stale approvals are rejected.
    Schedules execution immediately as a background task.
    """
    try:
        proposal = controller.approve(proposal_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Proposal not found")
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error))
    asyncio.create_task(controller.execute(proposal_id))
    return proposal


@app.get("/audit")
async def audit_log() -> dict[str, Any]:
    return {
        "controller_status": "healthy" if controller.healthy else "unhealthy",
        "audit": list(controller.audit),
    }


@app.get("/metrics", include_in_schema=False)
async def metrics():
    return metrics_response()
