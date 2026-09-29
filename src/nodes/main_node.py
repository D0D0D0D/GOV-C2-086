"""Cat2 boundary node delegating to the cached disaster workflow graph."""

from __future__ import annotations

from typing import Any, ClassVar

from framework.errors import SecurityViolationError
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.cat2_graph_skeleton.graph_node import DomainWorkflowGraphNode, ModeSpec
from src.graph.domain_workflow_graph import DisasterIntakeWorkflowGraph


class MainNode(DomainWorkflowGraphNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL
    error_strategy: ClassVar[str] = "propagate"
    propagate_hitl: ClassVar[bool] = False
    known_failure_codes: ClassVar[tuple[str, ...]] = (
        "E_INTERNAL_ENVELOPE", "E_MODE_UNKNOWN", "E_SCOPE_REQUIRED", "E_SCHEMA_UNKNOWN_FIELD",
    )
    mode_specs: ClassVar[dict[str, ModeSpec]] = {
        "ingest": ModeSpec(
            gate_kind="ingest",
            fields={
                "ingest_summary": "ingest_summary", "review_queue_delta": "review_queue_delta",
                "rejected": "rejected", "degradation_reason": "degradation_reason", "error_log": "error_log",
            },
            required_fields=("ingest_summary",),
        ),
        "invoke": ModeSpec(
            gate_kind="invoke",
            fields={
                "facility_status_snapshot": "facility_status_snapshot",
                "urgency_evaluations": "urgency_evaluations", "briefs": "briefs", "unresolved": "unresolved",
                "rejected": "rejected", "degradation_reason": "degradation_reason", "error_log": "error_log",
            },
            required_fields=("facility_status_snapshot", "urgency_evaluations", "briefs"),
        ),
        "feedback": ModeSpec(
            gate_kind="feedback",
            fields={
                "review_queue_delta": "review_queue_delta", "applied_count": "applied_count",
                "rejected_decisions": "rejected_decisions", "rejected": "rejected",
                "degradation_reason": "degradation_reason", "error_log": "error_log",
            },
            required_fields=("applied_count", "rejected_decisions"),
        ),
    }

    def __init__(self, *, inner_config: dict[str, Any] | None = None, llm=None) -> None:
        config = dict(inner_config or {})
        if llm is not None or "llm" not in config:
            config["llm"] = llm
        super().__init__(
            config=config,
            subgraph_factory=lambda child_config: DisasterIntakeWorkflowGraph(config=child_config),
            compile_subgraph=True,
            use_inner_checkpointer=False,
        )
        self._llm = config.get("llm")

    def _parent_config(self) -> dict[str, Any]:
        forwarded = dict(self._config)
        forwarded["memory_enabled"] = False
        forwarded["hitl"] = {"enabled": False}
        return forwarded

    def execute(self, state: dict) -> dict:
        mode = state.get("request_mode", state.get("mode", ""))
        emit_trace_event("subgraph_invoked", {"mode": mode, "skipped": state.get("status") == AgentStatus.ERROR.value}, state)
        if state.get("status") == AgentStatus.ERROR.value:
            return {}
        return super().execute(state)

    def extract_input(self, state: dict) -> str:
        payload_store = self._config.get("payload_store")
        if payload_store is None:
            raise SecurityViolationError("E_INTERNAL_ENVELOPE")
        envelope = {
            "mode": state["request_mode"],
            "scope": state["scope"],
            "request_clock": state["request_clock"],
            "caller_id": state.get("caller_id", ""),
            "report_refs": state.get("report_refs", []),
            "facility_id_snapshot": state.get("facility_id_snapshot", []),
            "as_of": state.get("as_of", state["request_clock"]),
            "decisions": state.get("decisions", []),
            "record_kind": state.get("record_kind"),
            "rejected": state.get("rejected", []),
        }
        return payload_store.put(
            envelope,
            scope=state["scope"],
            session_id=state["session_id"],
            envelope=True,
        )

    def merge_output(self, state: dict, sub_result: dict) -> dict:
        delta = super().merge_output(state, sub_result)
        delta.pop("node_history", None)
        if not delta.get("error_log"):
            delta.pop("error_log", None)
        return delta

    def _extra_security_gate_input(self, state: dict) -> dict:
        mode = state.get("request_mode")
        scope = state.get("scope")
        if mode not in self.mode_specs or not isinstance(scope, dict) or set(scope) != {"disaster_event_id"}:
            raise SecurityViolationError("E_INTERNAL_ENVELOPE")
        return state

    def _extra_security_gate_output(self, result: dict) -> dict:
        if not isinstance(result, dict):
            raise SecurityViolationError("E_INTERNAL_ENVELOPE")
        gate_kind = result.get("gate_kind")
        if gate_kind not in {spec.gate_kind for spec in self.mode_specs.values()}:
            raise SecurityViolationError("E_INTERNAL_ENVELOPE")
        return result
