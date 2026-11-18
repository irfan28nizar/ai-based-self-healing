from uuid import uuid4

from fastapi import FastAPI, Request
from pydantic import BaseModel, Field

from services.shared.failure_control import FailureConfiguration, FailureController
from services.shared.observability import (
    instrument_requests,
    metrics_response,
    record_business_event,
)

app = FastAPI(title="Payment Service", version="0.1.0")
failure_controller = FailureController("payment-service")


class PaymentRequest(BaseModel):
    user_id: str
    amount: float = Field(gt=0)


class PaymentResponse(BaseModel):
    transaction_id: str
    status: str


@app.middleware("http")
async def inject_failure(request: Request, call_next):
    return await failure_controller.intercept(request, call_next)


app.middleware("http")(instrument_requests("payment-service"))


@app.get("/health")
async def health_check() -> dict[str, str]:
    return {"service": "payment-service", "status": "healthy"}


@app.get("/metrics", include_in_schema=False)
async def metrics():
    return metrics_response()


@app.post("/payments", response_model=PaymentResponse, status_code=201)
async def create_payment(payment: PaymentRequest) -> PaymentResponse:
    record_business_event("payment-service", "payment_authorization", "authorized")
    return PaymentResponse(transaction_id=f"txn-{uuid4().hex[:12]}", status="authorized")


@app.get("/admin/failure", response_model=FailureConfiguration)
async def get_failure_configuration() -> FailureConfiguration:
    return failure_controller.configuration


@app.post("/admin/failure", response_model=FailureConfiguration)
async def set_failure_configuration(configuration: FailureConfiguration) -> FailureConfiguration:
    return failure_controller.configure(configuration)
