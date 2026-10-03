"""Routing depends only on validated decisions and the fixed evidence budget."""

import pytest

from incidentops.graph.routing import route_assessment


@pytest.mark.parametrize(
    ("decision", "round_number", "route"),
    [
        ("propose_remediation", 1, "end"),
        ("escalate", 1, "end"),
        ("need_more_evidence", 1, "collect"),
        ("need_more_evidence", 2, "limit"),
        ("propose_remediation", 2, "end"),
        ("escalate", 2, "end"),
    ],
)
def test_conditional_route(decision, round_number, route):
    assert (
        route_assessment(
            {
                "assessment_history": [{"decision": decision}],
                "evidence_round": round_number,
            }
        )
        == route
    )
