"""Synthetic checkout with two deployed implementations and runtime evidence."""

import asyncio
import os
from collections import deque
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Literal

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field


def timestamp():
    return datetime.now(UTC).isoformat()


def reset_state(state):
    # Called at startup or while holding the request/deployment lock.
    state.active_version = "v1"
    state.deployed_versions = {"v1"}
    state.deployment_history = deque(
        [{"version": "v1", "timestamp": timestamp()}], maxlen=100
    )
    state.requests_total = 0
    state.requests_5xx = 0
    state.logs = deque(maxlen=100)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.lock = asyncio.Lock()
    reset_state(app.state)
    async with httpx.AsyncClient(
        base_url=os.environ.get("INVENTORY_BASE_URL", "http://inventory:8000"),
        timeout=5,
        trust_env=False,
    ) as client:
        app.state.inventory = client
        yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


class CheckoutInput(BaseModel):
    sku: str = Field(min_length=1)
    quantity: int = Field(gt=0)


class DeploymentInput(BaseModel):
    version: Literal["v1", "v2"]


class RollbackInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    target_version: Literal["v1", "v2"]


def record(state, level, message, event, **fields):
    state.logs.append(
        {
            "timestamp": timestamp(),
            "level": level,
            "service": "checkout-api",
            "message": message,
            "event": event,
            **fields,
        }
    )


def failure(state, status, message, event, version, sku, **fields):
    if status >= 500:
        state.requests_5xx += 1
    record(
        state,
        "ERROR" if status >= 500 else "WARNING",
        message,
        event,
        version=version,
        sku=sku,
        status_code=status,
        **fields,
    )
    return JSONResponse({"detail": message}, status_code=status)


@app.post("/checkout")
async def checkout(payload: CheckoutInput, request: Request):
    state = request.app.state
    # Serialize this small environment's business/control operations so reset
    # cannot erase counters mid-request and deployment changes apply atomically.
    async with state.lock:
        version = state.active_version
        state.requests_total += 1
        record(
            state,
            "INFO",
            "Checkout started",
            "checkout_started",
            version=version,
            sku=payload.sku,
        )
        try:
            response = await state.inventory.get(f"/inventory/{payload.sku}")
            response.raise_for_status()
        except httpx.RequestError as error:
            return failure(
                state,
                502,
                "Inventory request failed",
                "downstream_error",
                version,
                payload.sku,
                error_type=type(error).__name__,
            )
        except httpx.HTTPStatusError as error:
            status = 404 if error.response.status_code == 404 else 502
            return failure(
                state,
                status,
                "Inventory response unsuccessful",
                "downstream_error",
                version,
                payload.sku,
                downstream_status=error.response.status_code,
            )

        stock = response.json()
        if stock["available"] < payload.quantity:
            return failure(
                state,
                409,
                "Insufficient inventory",
                "checkout_rejected",
                version,
                payload.sku,
            )
        try:
            # v2 mistakenly expects a renamed price field after a real inventory
            # response. The downstream service still exposes the v1 contract.
            price_field = "unit_price_cents" if version == "v1" else "unit_price"
            total_cents = stock[price_field] * payload.quantity
        except KeyError as error:
            return failure(
                state,
                500,
                "Checkout calculation failed",
                "checkout_error",
                version,
                payload.sku,
                error_type=type(error).__name__,
            )

        record(
            state,
            "INFO",
            "Checkout completed",
            "checkout_completed",
            version=version,
            sku=payload.sku,
            status_code=200,
        )
        return {
            "sku": payload.sku,
            "quantity": payload.quantity,
            "total_cents": total_cents,
            "version": version,
        }


@app.get("/__ops/health")
async def health():
    return {"service": "checkout-api", "status": "healthy"}


@app.get("/__ops/metrics")
async def metrics(request: Request):
    state = request.app.state
    async with state.lock:
        return {
            "requests_total": state.requests_total,
            "requests_5xx": state.requests_5xx,
        }


@app.get("/__ops/logs")
async def logs(request: Request):
    state = request.app.state
    async with state.lock:
        return list(state.logs)


@app.get("/__ops/deployments")
async def deployments(request: Request):
    state = request.app.state
    async with state.lock:
        return {
            "active_version": state.active_version,
            "deployment_history": list(state.deployment_history),
        }


@app.post("/__control/deploy")
async def deploy(payload: DeploymentInput, request: Request):
    state = request.app.state
    async with state.lock:
        state.active_version = payload.version
        state.deployed_versions.add(payload.version)
        state.deployment_history.append(
            {"version": payload.version, "timestamp": timestamp()}
        )
        return {"active_version": state.active_version}


@app.post("/__control/reset")
async def reset(request: Request):
    state = request.app.state
    async with state.lock:
        reset_state(state)
    return {"status": "reset", "active_version": "v1"}


@app.post("/__control/rollback")
async def rollback(payload: RollbackInput, request: Request):
    state = request.app.state
    async with state.lock:
        if payload.target_version not in state.deployed_versions:
            return JSONResponse(
                {"detail": "Rollback target was not previously deployed"},
                status_code=409,
            )
        previous = state.active_version
        applied = previous != payload.target_version
        if applied:
            state.active_version = payload.target_version
            state.deployment_history.append(
                {"version": payload.target_version, "timestamp": timestamp()}
            )
        return {
            "service": "checkout",
            "previous_version": previous,
            "active_version": state.active_version,
            "target_version": payload.target_version,
            "applied": applied,
        }
