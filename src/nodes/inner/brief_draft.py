"""LLM-assisted facility brief drafting with deterministic fallback."""

from __future__ import annotations

import json
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.llm_draft_stage.draft_node import DraftStageHooks, LlmDraftStageNode


_BRIEF_KEYS = {"facility_id", "finding", "required_actions", "claims"}
_CLAIM_KEYS = {"field_path", "observation_id"}
_FIELD_PATH_ROOTS = ("finding", "required_actions[i]")


class BriefDraftNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, *, config: dict[str, Any] | None = None) -> None:
        super().__init__()
        self._config = dict(config or {})

    def execute(self, state: dict) -> dict:
        if state.get("status") == AgentStatus.ERROR.value:
            emit_trace_event("brief_drafted", {"facility_count": 0, "upstream_error": True}, state)
            return {}

        def build_prompt(current: dict[str, Any]) -> str:
            return _build_brief_prompt(current)

        def build_fallback(current: dict[str, Any]) -> dict[str, Any]:
            return {
                "briefs": [
                    {
                        "facility_id": evaluation["facility_id"],
                        "finding": "",
                        "required_actions": [],
                        "claims": [],
                    }
                    for evaluation in current.get("urgency_evaluations", [])
                ]
            }

        def build_evidence(_current: dict[str, Any], draft: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
            claims = [
                claim
                for brief in draft.get("briefs", []) if isinstance(brief, dict)
                for claim in brief.get("claims", []) if isinstance(claim, dict)
            ]
            return claims, sorted({str(claim.get("observation_id")) for claim in claims if claim.get("observation_id")})

        part = LlmDraftStageNode(
            config=self._config,
            hooks=DraftStageHooks(build_prompt=build_prompt, build_fallback=build_fallback, build_evidence=build_evidence),
        )
        part_result = part.execute(state)
        unresolved = list(state.get("unresolved", []))
        degradations = list(state.get("degradation_reason", []))
        if part_result.get("status") == AgentStatus.ERROR.value:
            degradations.append("llm_contract_violation")
            for evaluation in state.get("urgency_evaluations", []):
                unresolved.append({"facility_id": evaluation["facility_id"], "reason_code": "E_LLM_CONTRACT", "note": "brief contract violation"})
            emit_trace_event("claim_unverified", {"reason_code": "E_LLM_CONTRACT"}, state)
            return {
                "briefs": [],
                "unresolved": unresolved,
                "degradation_reason": sorted(set(degradations)),
                "status": AgentStatus.SUCCESS.value,
            }
        if part_result.get("degradation_code") == "llm_not_configured":
            degradations.append("llm_unavailable")
            emit_trace_event("degradation_recorded", {"reason_code": "llm_unavailable"}, state)
        draft = part_result.get("draft", {})
        values = draft.get("briefs") if isinstance(draft, dict) and set(draft) == {"briefs"} else None
        if not isinstance(values, list):
            values = []
            degradations.append("llm_contract_violation")
        known = {item["facility_id"] for item in state.get("urgency_evaluations", [])}
        briefs: list[dict[str, Any]] = []
        seen: set[str] = set()
        for value in values:
            if not _valid_brief(value) or value["facility_id"] not in known or value["facility_id"] in seen:
                facility_id = value.get("facility_id", "") if isinstance(value, dict) else ""
                unresolved.append({"facility_id": facility_id, "reason_code": "E_LLM_CONTRACT", "note": "brief contract violation"})
                degradations.append("llm_contract_violation")
                emit_trace_event("claim_unverified", {"reason_code": "E_LLM_CONTRACT"}, state)
                continue
            seen.add(value["facility_id"])
            briefs.append(value)
        for facility_id in sorted(known - seen):
            if self._config.get("llm") is not None:
                unresolved.append({"facility_id": facility_id, "reason_code": "E_LLM_CONTRACT", "note": "brief missing"})
                degradations.append("llm_contract_violation")
        emit_trace_event("brief_drafted", {"facility_count": len(briefs)}, state)
        return {
            "briefs": briefs,
            "unresolved": unresolved,
            "degradation_reason": sorted(set(degradations)),
            "status": AgentStatus.SUCCESS.value,
        }


def _valid_brief(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != _BRIEF_KEYS:
        return False
    if not isinstance(value["facility_id"], str) or not isinstance(value["finding"], str):
        return False
    if not isinstance(value["required_actions"], list) or any(not isinstance(item, str) for item in value["required_actions"]):
        return False
    if not isinstance(value["claims"], list):
        return False
    for claim in value["claims"]:
        if not isinstance(claim, dict) or set(claim) != _CLAIM_KEYS:
            return False
        if not isinstance(claim["field_path"], str) or not isinstance(claim["observation_id"], str):
            return False
    return True


def _build_brief_prompt(current: dict[str, Any]) -> str:
    allowed_facility_ids = sorted(
        str(item["facility_id"])
        for item in current.get("urgency_evaluations", [])
        if isinstance(item, dict) and item.get("facility_id")
    )
    return json.dumps(
        {
            "task": "Draft grounded findings and required actions. Return the declared JSON object only.",
            "schema": {
                "briefs": [{
                    "facility_id": "string",
                    "finding": "string",
                    "required_actions": ["string"],
                    "claims": [{
                        "field_path": "string",
                        "observation_id": "string",
                    }],
                }]
            },
            "constraints": {
                "required_keys": {
                    "brief": sorted(_BRIEF_KEYS),
                    "claim": sorted(_CLAIM_KEYS),
                },
                "facility_id": "Use only a value from allowed_facility_ids and return one brief for each allowed facility.",
                "field_path": (
                    f"Point to one of {list(_FIELD_PATH_ROOTS)!r}, optionally with its sentence coordinate; "
                    "use the LLM schema names exactly."
                ),
                "evidence": (
                    "Every prose fragment must reference an observation_id from the same facility_status_snapshot; "
                    "return exactly the two declared claim keys and never invent an observation_id."
                ),
            },
            "allowed_facility_ids": allowed_facility_ids,
            "facility_status_snapshot": current.get("facility_status_snapshot", []),
            "urgency_evaluations": current.get("urgency_evaluations", []),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
