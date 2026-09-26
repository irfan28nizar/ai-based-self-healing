from __future__ import annotations

import asyncio
from enum import Enum

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.requests import Request
from starlette.types import ASGIApp


class FailureMode(str, Enum):
    unavailable = "unavailable"
    error = "error"
    slow = "slow"


class FailureConfiguration(BaseModel):
    enabled: bool = False
    mode: FailureMode = FailureMode.unavailable
    delay_seconds: float = Field(default=5, ge=0, le=60)


class FailureController:
    """Keeps a service's local, deliberately injected failure state."""

    def __init__(self, service_name: str) -> None:
        self.service_name = service_name
        self.configuration = FailureConfiguration()

    def configure(self, configuration: FailureConfiguration) -> FailureConfiguration:
        self.configuration = configuration
        return self.configuration

    async def intercept(self, request: Request, call_next: ASGIApp):
        if request.url.path.startswith("/admin/failure") or not self.configuration.enabled:
            return await call_next(request)

        if self.configuration.mode is FailureMode.slow:
            await asyncio.sleep(self.configuration.delay_seconds)
            return await call_next(request)

        status_code = 503 if self.configuration.mode is FailureMode.unavailable else 500
        return JSONResponse(
            status_code=status_code,
            content={
                "detail": f"Injected {self.configuration.mode.value} failure",
                "service": self.service_name,
            },
        )


def raise_dependency_failure(dependency: str, status_code: int) -> None:
    raise HTTPException(
        status_code=502,
        detail=f"{dependency} service failed with HTTP {status_code}",
    )
