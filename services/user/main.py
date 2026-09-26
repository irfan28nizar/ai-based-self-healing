from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from services.shared.failure_control import FailureConfiguration, FailureController

app = FastAPI(title="User Service", version="0.1.0")
failure_controller = FailureController("user-service")

USERS = {
    "user-1": {"id": "user-1", "name": "Asha Nair", "email": "asha@example.com"},
    "user-2": {"id": "user-2", "name": "Rahul Kumar", "email": "rahul@example.com"},
}


class User(BaseModel):
    id: str
    name: str
    email: str


@app.middleware("http")
async def inject_failure(request: Request, call_next):
    return await failure_controller.intercept(request, call_next)


@app.get("/health")
async def health_check() -> dict[str, str]:
    return {"service": "user-service", "status": "healthy"}


@app.get("/users/{user_id}", response_model=User)
async def get_user(user_id: str) -> dict[str, str]:
    user = USERS.get(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@app.get("/admin/failure", response_model=FailureConfiguration)
async def get_failure_configuration() -> FailureConfiguration:
    return failure_controller.configuration


@app.post("/admin/failure", response_model=FailureConfiguration)
async def set_failure_configuration(configuration: FailureConfiguration) -> FailureConfiguration:
    return failure_controller.configure(configuration)
