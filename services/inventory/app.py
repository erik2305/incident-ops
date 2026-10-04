"""Healthy synthetic inventory service; all evidence comes from handled traffic."""

import asyncio
from collections import deque
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

STOCK = {"SKU-001": 25, "SKU-002": 8}


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.lock = asyncio.Lock()
    app.state.requests_total = 0
    app.state.requests_5xx = 0
    app.state.logs = deque(maxlen=100)
    app.state.fault_mode = "normal"
    yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


def stock_for(sku):
    return {"sku": sku, "available": STOCK[sku], "unit_price_cents": 1999}


class FaultInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    mode: Literal["normal", "unavailable"]


def inventory_response(state, sku):
    if state.fault_mode == "unavailable":
        return JSONResponse({"detail": "Inventory unavailable"}, status_code=503)
    if sku not in STOCK:
        return JSONResponse({"detail": "SKU not found"}, status_code=404)
    return stock_for(sku)


@app.get("/inventory/{sku}")
async def inventory(sku: str, request: Request):
    state = request.app.state
    async with state.lock:
        state.requests_total += 1
        response = inventory_response(state, sku)
        status = response.status_code if isinstance(response, JSONResponse) else 200
        if status >= 500:
            state.requests_5xx += 1
        state.logs.append(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "level": "ERROR"
                if status >= 500
                else "INFO"
                if status == 200
                else "WARNING",
                "service": "inventory-api",
                "message": "Inventory unavailable"
                if status >= 500
                else "Inventory read"
                if status == 200
                else "SKU not found",
                "event": "inventory_read",
                "sku": sku,
                "status_code": status,
            }
        )
        return response


@app.get("/__ops/probe-stock")
async def probe_stock(request: Request):
    """Fixed observational inventory read for checkout's business probe."""
    state = request.app.state
    async with state.lock:
        return inventory_response(state, "SKU-001")


@app.post("/__control/fault")
async def fault(payload: FaultInput, request: Request):
    state = request.app.state
    async with state.lock:
        state.fault_mode = payload.mode
    return {"status": "configured"}


@app.get("/__ops/health")
async def health():
    return {"service": "inventory-api", "status": "healthy"}


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


@app.post("/__control/reset")
async def reset(request: Request):
    state = request.app.state
    async with state.lock:
        state.requests_total = 0
        state.requests_5xx = 0
        state.logs.clear()
        state.fault_mode = "normal"
    return {"status": "reset"}
