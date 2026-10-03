"""Project bounded graph updates and actual interrupts into safe native SSE."""

from contextlib import aclosing

from fastapi.sse import ServerSentEvent

from incidentops.api.models import (
    APIModel,
    ApprovalRequiredPayload,
    CompletedPayload,
    ErrorPayload,
    EventName,
    ProgressPayload,
    StartedPayload,
)
from incidentops.domain.actions import action_payload, validate_pending_action

NODES = frozenset(
    {
        "initialize_incident",
        "collect_initial_evidence",
        "assess_evidence",
        "collect_requested_evidence",
        "escalate_evidence_limit",
        "prepare_action",
        "approval_gate",
        "execute_action",
        "verify_recovery",
        "finalize_resolved",
        "finalize_verification_failure",
        "reject_action",
    }
)


def event(name: EventName, payload: APIModel) -> ServerSentEvent:
    return ServerSentEvent(
        event=name, data=payload.model_dump(mode="json", exclude_none=True)
    )


async def stream_incident(
    graph, incident_id, graph_input, runtime, *, starting, status
):
    """The response task owns the run; cancellation is never caught as an error."""
    config = {"configurable": {"thread_id": incident_id}}
    if starting:
        yield event("incident_started", StartedPayload(incident_id=incident_id))
    try:
        async with runtime as context:
            async with aclosing(
                graph.astream(
                    graph_input, config=config, context=context, stream_mode="updates"
                )
            ) as updates:
                async for update in updates:
                    for node, values in update.items():
                        if node == "__interrupt__":
                            continue
                        if node not in NODES:
                            raise ValueError("Unexpected graph update")
                        status = values.get("status", status)
                        yield event(
                            "progress",
                            ProgressPayload(
                                incident_id=incident_id, node=node, status=status
                            ),
                        )
            snapshot = await graph.aget_state(config)
        # Read the actual persisted interrupt, never infer it from status alone.
        if snapshot.interrupts:
            if len(snapshot.interrupts) != 1:
                raise ValueError("Unexpected interrupt count")
            payload = ApprovalRequiredPayload.model_validate(
                snapshot.interrupts[0].value
            )
            pending = validate_pending_action(
                incident_id, snapshot.values.get("pending_action")
            )
            if (
                payload.incident_id != incident_id
                or payload.action_id != pending["action_id"]
                or payload.fingerprint != pending["fingerprint"]
                or payload.action != action_payload(pending)
            ):
                raise ValueError(
                    "Interrupted action does not match persisted authority"
                )
            yield event("approval_required", payload)
        else:
            yield event(
                "completed",
                CompletedPayload(
                    incident_id=incident_id,
                    status=snapshot.values["status"],
                    escalation_reason=snapshot.values.get("escalation_reason"),
                ),
            )
    except Exception:  # noqa: BLE001 -- final transport boundary; cancellation propagates.
        # External exception text can contain credentials. Publish only fixed text.
        yield event("error", ErrorPayload(incident_id=incident_id))
