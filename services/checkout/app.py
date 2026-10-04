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


async def calculate_checkout(state, payload, version, *, observational=False):
    """Shared business calculation; callers decide whether to record traffic."""
    path = "/__ops/probe-stock" if observational else f"/inventory/{payload.sku}"
    try:
        response = await state.inventory.get(path)
        response.raise_for_status()
    except httpx.RequestError as error:
        return (
            502,
            "Inventory request failed",
            "downstream_error",
            {"error_type": type(error).__name__},
        )
    except httpx.HTTPStatusError as error:
        status = 404 if error.response.status_code == 404 else 502
        return (
            status,
            "Inventory response unsuccessful",
            "downstream_error",
            {"downstream_status": error.response.status_code},
        )
    try:
        stock = response.json()
        if (
            type(stock) is not dict
            or type(stock.get("available")) is not int
            or type(stock.get("unit_price_cents")) is not int
        ):
            raise ValueError("Invalid inventory contract")
    except ValueError:
        return 502, "Inventory response invalid", "downstream_error", {}
    if stock["available"] < payload.quantity:
        return 409, "Insufficient inventory", "checkout_rejected", {}
    try:
        # v2's actual defect runs here for ordinary requests AND probes.
        price_field = "unit_price_cents" if version == "v1" else "unit_price"
        total_cents = stock[price_field] * payload.quantity
    except KeyError as error:
        return (
            500,
            "Checkout calculation failed",
            "checkout_error",
            {"error_type": type(error).__name__},
        )
    return (
        200,
        "Checkout completed",
        "checkout_completed",
        {
            "sku": payload.sku,
            "quantity": payload.quantity,
            "total_cents": total_cents,
            "version": version,
        },
    )


@app.post("/checkout")
async def checkout(payload: CheckoutInput, request: Request):
    state = request.app.state
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
        status, message, event, data = await calculate_checkout(state, payload, version)
        if status != 200:
            return failure(state, status, message, event, version, payload.sku, **data)
        record(
            state,
            "INFO",
            message,
            event,
            version=version,
            sku=payload.sku,
            status_code=200,
        )
        return data


@app.get("/__ops/probe")
async def probe(request: Request):
    state = request.app.state
    async with state.lock:
        status, _, _, _ = await calculate_checkout(
            state,
            CheckoutInput(sku="SKU-001", quantity=1),
            state.active_version,
            observational=True,
        )
        return {"service": "checkout", "ok": status == 200, "observed_status": status}


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
