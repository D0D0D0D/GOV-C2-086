"""Load the single as-of facility snapshot for invoke mode."""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event


class StatusLoadNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, *, repository=None) -> None:
        super().__init__()
        self._repository = repository

    def execute(self, state: dict) -> dict:
        if state.get("status") == AgentStatus.ERROR.value:
            emit_trace_event("degradation_recorded", {"reason_code": "upstream_error"}, state)
            return {}
        try:
            registry = {row["facility_id"]: row for row in self._repository.load_registry()}
            statuses = {
                row["facility_id"]: row
                for row in self._repository.load_status(state["scope"], state.get("facility_id_snapshot", []))
            }
        except Exception:
            degradations = sorted(set([*state.get("degradation_reason", []), "repository_degraded"]))
            emit_trace_event("degradation_recorded", {"reason_code": "repository_degraded"}, state)
            return {
                "facility_status_snapshot": [],
                "degradation_reason": degradations,
                "status": AgentStatus.SUCCESS.value,
            }
        as_of = _parse_timestamp(state["as_of"])
        snapshot: list[dict[str, Any]] = []
        for facility_id in state.get("facility_id_snapshot", []):
            master = registry.get(facility_id)
            status = statuses.get(facility_id)
            if master is None or status is None:
                continue
            observations = [
                dict(item)
                for item in status["damage_observations"]
                if _parse_timestamp(item["observed_at"]) <= as_of
            ]
            snapshot.append(
                {
                    "facility_id": facility_id,
                    "facility_name": master["name"],
                    "importance": master["importance"],
                    "damage_observations": sorted(observations, key=lambda item: (item["observed_at"], item["observation_id"])),
                    "conflicts": [dict(item) for item in status["conflicts"]],
                }
            )
        emit_trace_event("status_persisted", {"loaded_count": len(snapshot), "read_only": True}, state)
        return {"facility_status_snapshot": snapshot, "status": AgentStatus.SUCCESS.value}


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
