import os
from uuid import uuid4

import httpx
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from services.shared.failure_control import (
    FailureConfiguration,
    FailureController,
    raise_dependency_failure,
)
from services.shared.observability import (
    instrument_requests,
    metrics_response,
    record_business_event,
)

app = FastAPI(title="Order Service", version="0.1.0")
failure_controller = FailureController("order-service")
USER_SERVICE_URL = os.getenv("USER_SERVICE_URL", "http://localhost:8001")
PAYMENT_SERVICE_URL = os.getenv("PAYMENT_SERVICE_URL", "http://localhost:8003")


class OrderRequest(BaseModel):
    user_id: str
    item: str = Field(min_length=1, max_length=120)
    amount: float = Field(gt=0)


class OrderResponse(BaseModel):
    order_id: str
    user_id: str
    item: str
    amount: float
    payment_transaction_id: str
    status: str


@app.middleware("http")
async def inject_failure(request: Request, call_next):
    return await failure_controller.intercept(request, call_next)


app.middleware("http")(instrument_requests("order-service"))


@app.get("/health")
async def health_check() -> dict[str, str]:
    return {"service": "order-service", "status": "healthy"}


@app.get("/metrics", include_in_schema=False)
async def metrics():
    return metrics_response()


@app.post("/orders", response_model=OrderResponse, status_code=201)
async def create_order(order: OrderRequest) -> OrderResponse:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            user_response = await client.get(f"{USER_SERVICE_URL}/users/{order.user_id}")
            if user_response.status_code == 404:
                record_business_event("order-service", "order_creation", "user_not_found")
                raise HTTPException(status_code=404, detail="User not found")
            if user_response.is_error:
                record_business_event("order-service", "order_creation", "dependency_error")
                raise_dependency_failure("User", user_response.status_code)

            payment_response = await client.post(
                f"{PAYMENT_SERVICE_URL}/payments",
                json={"user_id": order.user_id, "amount": order.amount},
            )
            if payment_response.is_error:
                record_business_event("order-service", "order_creation", "dependency_error")
                raise_dependency_failure("Payment", payment_response.status_code)
    except httpx.RequestError as error:
        record_business_event(
            "order-service", "order_creation", "dependency_unavailable"
        )
        raise HTTPException(
            status_code=502,
            detail=f"Dependency unavailable: {error.request.url}",
        ) from error

    transaction_id = payment_response.json()["transaction_id"]
    record_business_event("order-service", "order_creation", "confirmed")
    return OrderResponse(
        order_id=f"order-{uuid4().hex[:12]}",
        user_id=order.user_id,
        item=order.item,
        amount=order.amount,
        payment_transaction_id=transaction_id,
        status="confirmed",
    )


@app.get("/admin/failure", response_model=FailureConfiguration)
async def get_failure_configuration() -> FailureConfiguration:
    return failure_controller.configuration


@app.post("/admin/failure", response_model=FailureConfiguration)
async def set_failure_configuration(configuration: FailureConfiguration) -> FailureConfiguration:
    return failure_controller.configure(configuration)
