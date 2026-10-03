"""Healthy synthetic inventory service; all evidence comes from handled traffic."""

import asyncio
from collections import deque
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

STOCK = {"SKU-001": 25, "SKU-002": 8}


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.lock = asyncio.Lock()
    app.state.requests_total = 0
    app.state.requests_5xx = 0
    app.state.logs = deque(maxlen=100)
    yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/inventory/{sku}")
async def inventory(sku: str, request: Request):
    state = request.app.state
    async with state.lock:
        state.requests_total += 1
        known = sku in STOCK
        state.logs.append(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "level": "INFO" if known else "WARNING",
                "service": "inventory-api",
                "message": "Inventory read" if known else "SKU not found",
                "event": "inventory_read",
                "sku": sku,
                "status_code": 200 if known else 404,
            }
        )
        if not known:
            return JSONResponse({"detail": "SKU not found"}, status_code=404)
        return {"sku": sku, "available": STOCK[sku], "unit_price_cents": 1999}


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
    return {"status": "reset"}
