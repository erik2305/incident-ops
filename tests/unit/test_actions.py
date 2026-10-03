"""Canonical executable payload and stable, incident-bound identity."""

import pytest

from incidentops.domain.actions import (
    ActionIntegrityError,
    action_fingerprint,
    materialize_action,
    validate_pending_action,
)


def test_fingerprint_canonicalizes_key_order_and_covers_executable_arguments():
    payload = {
        "action": "rollback_deployment",
        "service": "checkout",
        "target_version": "v1",
    }
    assert action_fingerprint(payload) == action_fingerprint(
        dict(reversed(list(payload.items())))
    )
    assert action_fingerprint(payload) != action_fingerprint(
        {**payload, "target_version": "v2"}
    )
    assert action_fingerprint(payload) != action_fingerprint(
        {**payload, "service": "inventory"}
    )
    assert action_fingerprint(payload) != action_fingerprint(
        {**payload, "action": "restart"}
    )


def test_identity_is_stable_and_bound_to_incident_and_action():
    payload = {
        "action": "rollback_deployment",
        "service": "checkout",
        "target_version": "v1",
    }
    first = materialize_action("INC-001", payload)
    assert first == materialize_action("INC-001", payload)
    assert first["action_id"] != materialize_action("INC-002", payload)["action_id"]
    assert (
        first["action_id"]
        != materialize_action("INC-001", {**payload, "target_version": "v2"})[
            "action_id"
        ]
    )
    assert validate_pending_action("INC-001", first) == first


@pytest.mark.parametrize(
    "field", ["target_version", "fingerprint", "action_id", "service"]
)
def test_corrupted_action_is_never_repaired(field):
    pending = materialize_action(
        "INC-001",
        {
            "action": "rollback_deployment",
            "service": "checkout",
            "target_version": "v1",
        },
    )
    pending[field] = "changed"
    with pytest.raises(ActionIntegrityError):
        validate_pending_action("INC-001", pending)
