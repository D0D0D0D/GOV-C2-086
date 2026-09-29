"""Deterministic municipality-approved urgency-rule evaluation."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event


_SEVERITY_RANK = {
    "unknown": 0, "no_visible_damage": 1, "utility_outage": 2, "partial_damage": 3, "structural_damage": 4,
}
_URGENCY_RANK = {"normal": 0, "high": 1, "immediate": 2}


class UrgencyEvaluateNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, *, config: dict[str, Any] | None = None) -> None:
        super().__init__()
        self._config = dict(config or {})

    def execute(self, state: dict) -> dict:
        if state.get("status") == AgentStatus.ERROR.value:
            emit_trace_event("urgency_evaluated", {"facility_count": 0, "upstream_error": True}, state)
            return {}
        policy = self._config.get("urgency_rules", {"default_urgency": "normal", "rules": []})
        evaluations: list[dict[str, Any]] = []
        for facility in state.get("facility_status_snapshot", []):
            by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for observation in facility["damage_observations"]:
                by_category[observation["category"]].append(observation)
            category_results: list[tuple[str, str, int]] = []
            for category, observations in sorted(by_category.items()):
                latest_at = max(item["observed_at"] for item in observations)
                latest = sorted(
                    [item for item in observations if item["observed_at"] == latest_at],
                    key=lambda item: item["observation_id"],
                )
                aggregate = {
                    "importance": facility["importance"],
                    "category": category,
                    "severity_observed": max(
                        (item["severity_observed"] for item in latest), key=lambda value: _SEVERITY_RANK[value]
                    ),
                    "access_blocked": any(item["access_blocked"] for item in latest),
                }
                urgency, rule_id, index = _apply_rules(aggregate, policy)
                category_results.append((urgency, rule_id, index))
            if not category_results:
                continue
            urgency, rule_id, _ = sorted(
                category_results, key=lambda item: (-_URGENCY_RANK[item[0]], item[2], item[1])
            )[0]
            conflict_pending = any(conflict.get("state") == "open" for conflict in facility.get("conflicts", []))
            evaluations.append(
                {
                    "facility_id": facility["facility_id"],
                    "urgency": urgency,
                    "applied_rule_id": rule_id,
                    "conflict_pending": conflict_pending,
                }
            )
        evaluations.sort(key=lambda item: item["facility_id"])
        emit_trace_event("urgency_evaluated", {"facility_count": len(evaluations)}, state)
        return {"urgency_evaluations": evaluations, "status": AgentStatus.SUCCESS.value}


def _apply_rules(aggregate: dict[str, Any], policy: dict[str, Any]) -> tuple[str, str, int]:
    for index, rule in enumerate(policy["rules"]):
        matched = True
        for key, expected in rule["when"].items():
            if isinstance(expected, list):
                matched = matched and aggregate[key] in expected
            else:
                matched = matched and aggregate[key] == expected
        if matched:
            return rule["urgency"], rule["id"], index
    return policy["default_urgency"], "U_DEFAULT", len(policy["rules"])
