"""Outer fixed-backbone graph for FacilityInspectionIntakeAgent."""

from __future__ import annotations

import re
from typing import Any

from framework.errors import SecurityViolationError
from framework.graph.agent_base_graph import AgentBaseGraph

from src.nodes.main_node import MainNode
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.payload_store.payload_store import PayloadStore, is_reference
from src.schemas.state import State
from src.services.config_validation import validate_domain_config
from src.services.payload_broker import ScopedPayloadBroker
from src.services.repository_factory import create_repository


class Graph(AgentBaseGraph):
    @property
    def name(self) -> str:
        return "FacilityInspectionIntakeAgent"

    @property
    def state_schema(self) -> type:
        return State

    def _validate_config(self) -> None:
        super()._validate_config()
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
        super().register_nodes()
        config = self._config_snapshot
        payload_store = config["payload_store"]
        self._nodes["pre_process"] = PreProcessNode(payload_store=payload_store)
        self._nodes["main"] = MainNode(inner_config=config)
        self._nodes["post_process"] = PostProcessNode(config=config)

    def invoke(
        self,
        user_input: str,
        session_id: str = "",
        ctx=None,
        input_context: dict | None = None,
    ) -> dict:
        if not is_reference(user_input):
            raise SecurityViolationError("E_DIRECT_INVOKE_FORBIDDEN")
        return super().invoke(user_input, session_id=session_id, ctx=ctx, input_context=input_context)

    def get_output(self, state: State) -> dict:
        status = state.get("status")
        formatted = state.get("formatted_output")
        if not isinstance(formatted, dict):
            formatted = {
                "mode": state.get("request_mode", state.get("mode", "")),
                "generated_at": state.get("request_clock", ""),
                "error_log": _normalise_errors(state.get("error_log", [])),
                "rejected": state.get("rejected", []),
            }
        return {
            "output": formatted,
            "status": status,
            "trace_id": state.get("trace_id"),
            "correlation_id": state.get("correlation_id"),
            "node_history": state.get("node_history", []),
            "error_log": _normalise_errors(state.get("error_log", [])),
        }


def _normalise_errors(values: Any) -> list[dict[str, Any]]:
    if not isinstance(values, list):
        values = []
    output: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for value in values:
        text = value if isinstance(value, str) else ""
        lowered = text.casefold()
        if "timeout" in lowered:
            category = "provider_timeout"
        elif "auth" in lowered:
            category = "provider_auth"
        elif "rate" in lowered:
            category = "provider_rate_limit"
        elif "repository" in lowered:
            category = "repository_error"
        elif "validation" in lowered or "e_" in lowered:
            category = "validation_error"
        elif "llm" in lowered or "provider" in lowered:
            category = "provider_contract"
        else:
            category = "internal_error"
        match = re.search(r"\b([A-Za-z]+(?:Error|Exception))\b", text)
        error_type = match.group(1) if match else "RuntimeError"
        key = (category, error_type)
        if key not in seen:
            seen.add(key)
            output.append({"category": category, "type": error_type})
    return output
