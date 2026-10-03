"""Async HTTP/SSE transport for the existing checkpointed graph."""

from collections.abc import AsyncIterator
from contextlib import aclosing, asynccontextmanager
from typing import Annotated
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.sse import EventSourceResponse, ServerSentEvent
from langgraph.types import Command

from incidentops.api.models import (
    ApprovalRequest,
    IncidentResult,
    IncidentView,
    StartIncident,
)
from incidentops.api.runtime import APIConfig, RuntimeFactories
from incidentops.api.streaming import stream_incident
from incidentops.graph import build_graph
from incidentops.persistence import open_checkpointer


def create_app(
    config: APIConfig | None = None,
    *,
    factories: RuntimeFactories | None = None,
    checkpointer_factory=open_checkpointer,
) -> FastAPI:
    """Open one saver at startup; checkpoint schema setup is explicit provisioning."""
    config = config if config is not None else APIConfig.from_environment()
    factories = factories if factories is not None else RuntimeFactories()

    @asynccontextmanager
    async def lifespan(app):
        async with checkpointer_factory(config.database_url) as saver:
            app.state.graph = build_graph(checkpointer=saver)
            app.state.active_incidents = set()
            yield

    app = FastAPI(
        title="IncidentOps",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    async def reserve_incident(request: Request):
        incident_id = request.path_params.get("incident_id") or str(uuid4())
        active = request.app.state.active_incidents
        if incident_id in active:
            raise HTTPException(409, "An operation for this incident is already active")
        active.add(incident_id)
        try:
            yield incident_id
        finally:
            active.discard(incident_id)

    async def snapshot_for(incident_id: str):
        try:
            snapshot = await app.state.graph.aget_state(
                {"configurable": {"thread_id": incident_id}}
            )
        except Exception:  # noqa: BLE001 -- public boundary must suppress dependency secrets.
            raise HTTPException(
                503, "Incident state is temporarily unavailable"
            ) from None
        if not snapshot.values:
            raise HTTPException(404, "Incident not found")
        return snapshot

    @app.post("/incidents", response_class=EventSourceResponse)
    async def start_incident(
        payload: StartIncident,
        incident_id: Annotated[str, Depends(reserve_incident, scope="request")],
    ) -> AsyncIterator[ServerSentEvent]:
        async with aclosing(
            stream_incident(
                app.state.graph,
                incident_id,
                {"incident_id": incident_id, **payload.model_dump()},
                factories.start(config),
                starting=True,
                status="investigating",
            )
        ) as events:
            async for item in events:
                yield item

    @app.get(
        "/incidents/{incident_id}",
        response_model=IncidentView,
        response_model_exclude_none=True,
    )
    async def get_incident(incident_id: str):
        state = (await snapshot_for(incident_id)).values
        return IncidentView(
            **{key: state[key] for key in IncidentView.model_fields if key in state}
        )

    async def approval_preflight(
        incident_id: str,
        payload: ApprovalRequest,
        _reservation: Annotated[str, Depends(reserve_incident, scope="request")],
    ):
        snapshot = await snapshot_for(incident_id)
        pending = snapshot.values.get("pending_action")
        if (
            snapshot.values.get("status") != "awaiting_approval"
            or not pending
            or not snapshot.interrupts
        ):
            raise HTTPException(409, "Incident is not awaiting approval")
        if payload.action_id != pending["action_id"]:
            raise HTTPException(409, "Action ID does not match the pending action")
        return incident_id, payload, snapshot.values["status"]

    @app.post("/incidents/{incident_id}/approval", response_class=EventSourceResponse)
    async def approve_incident(
        approval: Annotated[
            tuple[str, ApprovalRequest, str], Depends(approval_preflight)
        ],
    ) -> AsyncIterator[ServerSentEvent]:
        incident_id, payload, status = approval
        async with aclosing(
            stream_incident(
                app.state.graph,
                incident_id,
                Command(resume=payload.model_dump()),
                factories.approval(config, payload.decision),
                starting=False,
                status=status,
            )
        ) as events:
            async for item in events:
                yield item

    @app.get(
        "/incidents/{incident_id}/result",
        response_model=IncidentResult,
        response_model_exclude_none=True,
    )
    async def get_result(incident_id: str):
        state = (await snapshot_for(incident_id)).values
        if state["status"] not in ("resolved", "escalated"):
            raise HTTPException(409, f"Incident is currently {state['status']}")
        history = state.get("assessment_history", [])
        result = {
            "incident_id": incident_id,
            "status": state["status"],
            "root_cause": history[-1]["root_cause"] if history else None,
        }
        if state["status"] == "resolved":
            result.update(
                approved_action=state["pending_action"],
                execution_record=state["execution_record"],
                verification_result=state["verification_result"],
            )
        else:
            result["escalation_reason"] = state.get("escalation_reason")
        return IncidentResult(**result)

    @app.get("/health")
    async def health():
        return {"status": "healthy"}

    return app
