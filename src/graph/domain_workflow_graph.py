"""Custom-topology inner graph for the three mutually exclusive modes."""

from __future__ import annotations

from framework.graph.base_graph import BaseGraph
from langgraph.graph import END, START

from src.nodes.inner.brief_draft import BriefDraftNode
from src.nodes.inner.dispatch import DispatchNode
from src.nodes.inner.entity_resolve import EntityResolveNode
from src.nodes.inner.feedback_apply import FeedbackApplyNode
from src.nodes.inner.intake_extract import IntakeExtractNode
from src.nodes.inner.reconcile_persist import ReconcilePersistNode
from src.nodes.inner.status_load import StatusLoadNode
from src.nodes.inner.urgency_evaluate import UrgencyEvaluateNode
from src.payload_store.payload_store import PayloadStore
from src.schemas.state import DisasterIntakeWorkflowState
from src.services.config_validation import validate_domain_config
from src.services.payload_broker import ScopedPayloadBroker
from src.services.repository_factory import create_repository


class DisasterIntakeWorkflowGraph(BaseGraph):
    @property
    def name(self) -> str:
        return "disaster_intake_workflow"

    @property
    def state_schema(self) -> type:
        return DisasterIntakeWorkflowState

    def _validate_config(self) -> None:
        snapshot = validate_domain_config(self.config)
        repository = create_repository(snapshot)
        payload_store = snapshot.get("payload_store")
        if payload_store is None:
            payload_store = ScopedPayloadBroker(
                PayloadStore(
                    scope_keys=("disaster_event_id",),
                    ttl_seconds=snapshot["payload_ttl_seconds"],
                )
            )
        snapshot["repository"] = repository
        snapshot["payload_store"] = payload_store
        self.config = snapshot
        self._config_snapshot = dict(snapshot)

    def register_nodes(self) -> None:
        config = self._config_snapshot
        repository = config["repository"]
        payload_store = config["payload_store"]
        self._nodes = {
            "dispatch": DispatchNode(payload_store=payload_store),
            "intake_extract": IntakeExtractNode(config=config, payload_store=payload_store, repository=repository),
            "entity_resolve": EntityResolveNode(config=config, repository=repository),
            "reconcile_persist": ReconcilePersistNode(repository=repository),
            "status_load": StatusLoadNode(repository=repository),
            "urgency_evaluate": UrgencyEvaluateNode(config=config),
            "brief_draft": BriefDraftNode(config=config),
            "feedback_apply": FeedbackApplyNode(config=config, repository=repository, payload_store=payload_store),
        }

    def add_edges(self) -> None:
        self._sg.add_edge(START, "dispatch")
        self._sg.add_conditional_edges(
            "dispatch",
            self.route,
            {
                "ingest": "intake_extract",
                "invoke": "status_load",
                "feedback": "feedback_apply",
                "error": END,
            },
        )
        self._sg.add_edge("intake_extract", "entity_resolve")
        self._sg.add_edge("entity_resolve", "reconcile_persist")
        self._sg.add_edge("reconcile_persist", END)
        self._sg.add_edge("status_load", "urgency_evaluate")
        self._sg.add_edge("urgency_evaluate", "brief_draft")
        self._sg.add_edge("brief_draft", END)
        self._sg.add_edge("feedback_apply", END)

    def route(self, state: DisasterIntakeWorkflowState) -> str:
        if state.get("status") == "error":
            return "error"
        mode = state.get("mode")
        return mode if mode in {"ingest", "invoke", "feedback"} else "error"

    def get_output(self, state: DisasterIntakeWorkflowState) -> dict:
        common = {
            "mode": state.get("mode", ""),
            "status": state.get("status"),
            "trace_id": state.get("trace_id", ""),
            "correlation_id": state.get("correlation_id", ""),
            "degradation_reason": state.get("degradation_reason", []),
            "rejected": state.get("rejected", []),
        }
        if state.get("status") == "error":
            return {**common, "error_log": state.get("error_log", [])}
        mode = state.get("mode")
        if mode == "ingest":
            return {
                **common,
                "ingest_summary": state.get("ingest_summary", {}),
                "review_queue_delta": state.get("review_queue_delta", []),
            }
        if mode == "invoke":
            return {
                **common,
                "facility_status_snapshot": state.get("facility_status_snapshot", []),
                "urgency_evaluations": state.get("urgency_evaluations", []),
                "briefs": state.get("briefs", []),
                "unresolved": state.get("unresolved", []),
            }
        if mode == "feedback":
            return {
                **common,
                "review_queue_delta": state.get("review_queue_delta", []),
                "applied_count": state.get("applied_count", 0),
                "rejected_decisions": state.get("rejected_decisions", []),
            }
        return {**common, "status": "error", "error_log": ["validation_error:ValueError:E_MODE_UNKNOWN"]}
