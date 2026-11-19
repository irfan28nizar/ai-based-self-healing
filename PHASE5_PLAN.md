# Phase 5 Plan: Guarded Recovery Controller

## Goal

Turn only a small, pre-approved set of high-confidence diagnoses into bounded,
auditable recovery actions. The controller starts in dry-run mode, requires
explicit human approval per proposal, enforces cooldowns and a kill switch, and
writes a complete audit trail. No Docker socket access, no arbitrary shell
commands, no credentials in config.

## Implementation status

Implemented in the current stack: a recovery controller service polls the Phase 4
analyzer every 15 seconds, runs a seven-gate policy engine, and creates proposals
when a high-confidence diagnosis maps to an allowlisted action. Proposals expose
a read-only view and require a human `POST /proposals/{id}/approve` call before
any action fires. After the action, the controller observes a bounded verification
window and reports `succeeded`, `failed`, or `unknown`. Dry-run mode is on by
default; the kill switch is off. All settings are in `docker-compose.yml`. State
is in memory.

## Action registry

| Action ID | Target service | Endpoint | Payload | Postcondition |
| --- | --- | --- | --- | --- |
| `clear_payment_failure` | payment-service | `POST /admin/failure` | `{"enabled": false}` | `up{job="payment-service"} == 1` |
| `clear_user_failure` | user-service | `POST /admin/failure` | `{"enabled": false}` | `up{job="user-service"} == 1` |
| `clear_order_failure` | order-service | `POST /admin/failure` | `{"enabled": false}` | `up{job="order-service"} == 1` |

## Policy gates (in order)

1. Kill switch not active.
2. Controller inputs healthy (analyzer reachable and reporting `healthy`).
3. Diagnosis status is `diagnosed` (not `insufficient_evidence`).
4. Leading hypothesis confidence is in the configured allowlist (default: `high`).
5. Cause string maps to an allowlisted action.
6. No pending, approved, or executing proposal exists for the same action.
7. Cooldown window has fully elapsed since the last completed action.

## Proposal lifecycle

```
evaluate() → PENDING (or DRY_RUN) → approve() → APPROVED
                                                     │
                                             expiry check
                                             ├─ failed → EXPIRED
                                             └─ passed → execute() → EXECUTING
                                                                        │
                                                                verify window
                                                                ├─ SUCCEEDED
                                                                ├─ FAILED
                                                                └─ UNKNOWN
```

## Configuration

All settings are environment variables in `docker-compose.yml`:

| Variable | Default | Description |
| --- | --- | --- |
| `RC_DRY_RUN` | `true` | Proposes but never executes. |
| `RC_KILL_SWITCH` | `false` | Halts all activity immediately. |
| `RC_EVALUATION_INTERVAL_SECONDS` | `15` | Polling interval. |
| `RC_REQUEST_TIMEOUT_SECONDS` | `3` | HTTP client timeout. |
| `RC_APPROVAL_EXPIRY_SECONDS` | `120` | Proposal is unapprovable after this. |
| `RC_COOLDOWN_SECONDS` | `300` | Minimum gap between repeated actions. |
| `RC_ALLOWED_CONFIDENCE` | `high` | Comma-separated confidence allowlist. |
| `RC_VERIFY_WINDOW_SECONDS` | `60` | Postcondition verification window. |
| `RC_VERIFY_POLL_SECONDS` | `5` | Interval between postcondition polls. |

## Prometheus metrics exported

| Metric | Type | Labels |
| --- | --- | --- |
| `rc_controller_healthy` | Gauge | — |
| `rc_kill_switch_active` | Gauge | — |
| `rc_dry_run_active` | Gauge | — |
| `rc_proposals_created_total` | Counter | `service`, `action` |
| `rc_proposals_denied_total` | Counter | `service`, `reason` |
| `rc_actions_total` | Counter | `service`, `action`, `result` |

## Acceptance criteria

- Dry-run proposes but changes nothing. Status is `dry_run`, not `pending`.
- No action runs without a valid, unexpired `POST /proposals/{id}/approve` call.
- Only the three allowlisted `/admin/failure` calls are ever executed.
- Repeated incidents respect the configured cooldown.
- Audit records capture proposal creation, approval, execution, and outcome.
- An unavailable or unhealthy analyzer blocks proposal creation and marks the
  controller unhealthy.
- Kill switch set to `true` stops all proposal creation and execution immediately.
- Verification reports `succeeded`, `failed`, or `unknown`; never claims success
  from the HTTP call result alone.
- Metrics remain bounded; no per-request or customer identifiers.

## Out of scope

- Arbitrary commands, Docker socket access, or container restarts.
- Autonomous retries, rollbacks, or approval without human input.
- Credentials or secrets in config or logs.
- Production access or authentication (planned for Phase 8).
- Persistent audit storage (planned for Phase 7).

## Known limitations

- State is in memory; proposals and audit log clear on restart.
- The confidence allowlist defaults to `high` only. Payment-service failures
  produce `high` confidence when the target is also down; degradation-only
  diagnoses produce `medium` and require `RC_ALLOWED_CONFIDENCE=high,medium`.
- Verification uses `up{job=...}` scrape health, not application-level health.
  A service may be scrape-reachable but still misbehaving.

## Exit criteria for Phase 6

Phase 5 is ready to inform a digital twin and simulation exercise when the
guarded recovery lifecycle (propose, approve, execute, verify) is demonstrably
stable across the known failure scenarios and uncertainty cases are explicit.
Phase 6 can then replay detector, analyzer, and controller decisions against
deterministic scenario timelines without touching live services.
