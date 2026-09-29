"""Flat, checkpoint-safe state contracts for GOV-C2-086."""

from typing import Any

from framework.schemas.agent_state import AgentState


class State(AgentState):
    """Outer fixed-backbone state.

    Every value is JSON serialisable.  Raw reports, adjudication notes,
    credentials, service instances, and InvocationContext never belong here.
    """

    request_mode: str
    mode: str
    scope: dict[str, str]
    request_clock: str
    report_refs: list[dict[str, Any]]
    facility_id_snapshot: list[str]
    as_of: str
    decisions: list[dict[str, Any]]
    record_kind: str | None
    rejected: list[dict[str, Any]]
    ingest_summary: dict[str, Any]
    facility_status_snapshot: list[dict[str, Any]]
    urgency_evaluations: list[dict[str, Any]]
    briefs: list[dict[str, Any]]
    review_queue_delta: list[dict[str, Any]]
    unresolved: list[dict[str, Any]]
    rejected_decisions: list[dict[str, Any]]
    applied_count: int
    degradation_reason: list[str]
    gate_kind: str


class DisasterIntakeWorkflowState(State):
    """Inner custom-topology state; declared separately for route projection."""

    extracted_observations: list[dict[str, Any]]
    resolved_observations: list[dict[str, Any]]
