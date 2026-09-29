"""Apply asynchronous human adjudication through one atomic repository call."""

from __future__ import annotations

import copy
import json
import unicodedata
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from framework.security import detect_pii
from framework.security.pii_masking import mask_pii
from shared.utils.audit_logger import emit_trace_event

from src.feedback_intake.feedback_node import FeedbackIntakeNode
from src.feedback_intake.feedback_service import (
    FeedbackIntakeService,
    FeedbackRejectedError,
    InMemoryFeedbackReceiptStore,
)
from src.services.feedback_adapter import RepositoryFeedbackLedgerAdapter
from src.services.domain_utils import stable_id


_INJECTION_TERMS = ("ignore previous", "ignore all previous", "以後の指示", "システムプロンプト", "system prompt")


class FeedbackApplyNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, *, config: dict[str, Any] | None = None, repository=None, payload_store=None) -> None:
        super().__init__()
        self._config = dict(config or {})
        self._repository = repository
        self._payload_store = payload_store
        self._part_node = None
        if repository is not None and payload_store is not None:
            service = FeedbackIntakeService(
                ledger=RepositoryFeedbackLedgerAdapter(repository),
                receipt_store=InMemoryFeedbackReceiptStore(),
            )
            self._part_node = FeedbackIntakeNode(
                service=service,
                payload_store=payload_store,
                scope_keys=("disaster_event_id",),
                payload_ref_key="note_ref",
            )

    def execute(self, state: dict) -> dict:
        if state.get("status") == AgentStatus.ERROR.value:
            emit_trace_event("feedback_applied", {"applied_count": 0, "upstream_error": True}, state)
            return {}
        scope = state["scope"]
        event_id = scope["disaster_event_id"]
        caller_id = state.get("caller_id", "")
        queue = {item["queue_id"]: item for item in self._repository.load_review_queue(scope)}
        statuses = {item["facility_id"]: item for item in self._repository.load_status(scope)}
        registry_ids = {item["facility_id"] for item in self._repository.load_registry()}
        resolutions: dict[str, dict[str, Any]] = {}
        status_updates: dict[str, dict[str, Any]] = {}
        audits: list[dict[str, Any]] = []
        delta: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []

        for index, decision in enumerate(state.get("decisions", [])):
            note_raw: str | None = None
            note_safe: str | None = None
            try:
                note_ref = decision.get("note_ref")
                if note_ref is not None:
                    raw = self._payload_store.resolve(
                        note_ref, scope=scope, session_id=state["session_id"], consume=False
                    )
                    if not isinstance(raw, str):
                        raise FeedbackRejectedError("E_INTERNAL_ENVELOPE")
                    note_raw = raw
                    note_safe = _safe_note(raw, self._config.get("max_note_chars", 500))
                if decision.get("facility_id") and decision["facility_id"] not in registry_ids:
                    raise FeedbackRejectedError("E_FACILITY_UNKNOWN")
                if self._part_node is None:
                    raise FeedbackRejectedError("feedback_service_unavailable")
                target_for_part = decision.get("queue_id") or decision.get("conflict_id")
                part_ref = self._payload_store.put(
                    json.dumps(
                        {
                            "record_id": target_for_part,
                            "feedback_seq": stable_id(state["request_clock"], index),
                            "verdict_code": decision["action"],
                            "rationale": note_safe or "",
                            # Never trust a client-supplied actor.  This value is
                            # overwritten from the InvocationContext-derived state.
                            "decided_by": caller_id,
                            "decided_at": state["request_clock"],
                        },
                        ensure_ascii=False,
                    ),
                    scope=scope,
                    session_id=state["session_id"],
                )
                part_result = self._part_node.execute(
                    {
                        "status": "success",
                        "note_ref": part_ref,
                        "session_id": state["session_id"],
                        "disaster_event_id": event_id,
                        "correlation_id": state.get("correlation_id", ""),
                    }
                )
                self._payload_store.delete(part_ref, scope=scope, session_id=state["session_id"])
                if part_result.get("status") == AgentStatus.ERROR.value:
                    raise FeedbackRejectedError("feedback_target_not_owned")
                if decision.get("queue_id"):
                    target = queue.get(decision["queue_id"])
                    if target is None:
                        raise FeedbackRejectedError("E_DECISION_TARGET")
                    if target["state"] != "open":
                        raise FeedbackRejectedError("E_ALREADY_RESOLVED")
                    before = copy.deepcopy(target)
                    target = copy.deepcopy(target)
                    target.update(
                        {
                            "state": "resolved",
                            "resolved_by": caller_id,
                            "resolved_at": state["request_clock"],
                            "reason_note": note_safe or "",
                        }
                    )
                    resolutions[target["queue_id"]] = target
                    queue[target["queue_id"]] = target
                    action = "correct_resolution" if decision["action"] in {"accept_candidate", "assign_facility"} else "resolve_queue"
                    target_id = target["queue_id"]
                else:
                    found = _find_conflict(statuses, decision["conflict_id"])
                    if found is None:
                        raise FeedbackRejectedError("E_DECISION_TARGET")
                    facility_id, status, conflict = found
                    if conflict["state"] != "open":
                        raise FeedbackRejectedError("E_ALREADY_RESOLVED")
                    keep = set(decision["keep_observation_ids"])
                    if not keep or not keep <= set(conflict["observation_ids"]):
                        raise FeedbackRejectedError("E_DECISION_FIELD")
                    before = copy.deepcopy(conflict)
                    updated_status = copy.deepcopy(status)
                    updated_status["damage_observations"] = [
                        item
                        for item in updated_status["damage_observations"]
                        if item["observation_id"] not in set(conflict["observation_ids"]) - keep
                    ]
                    for item in updated_status["conflicts"]:
                        if item["conflict_id"] == conflict["conflict_id"]:
                            item["state"] = "resolved"
                    updated_status["updated_at"] = state["request_clock"]
                    status_updates[facility_id] = updated_status
                    statuses[facility_id] = updated_status
                    target = next(item for item in updated_status["conflicts"] if item["conflict_id"] == conflict["conflict_id"])
                    action = "resolve_conflict"
                    target_id = conflict["conflict_id"]
                audit = {
                    "audit_id": stable_id(event_id, action, target_id, caller_id, state["request_clock"]),
                    "disaster_event_id": event_id,
                    "action": action,
                    "target_id": target_id,
                    "before": before,
                    "after": copy.deepcopy(target),
                    "actor": caller_id,
                    "at": state["request_clock"],
                    "note_raw": note_raw,
                    "note_audit": note_safe,
                }
                audits.append(audit)
                delta.append({"target_id": target_id, "state": "resolved", "resolved_by": caller_id, "reason_note": note_safe or ""})
            except FeedbackRejectedError as exc:
                rejected.append({"index": index, "reason_code": str(exc)})
                emit_trace_event("feedback_rejected", {"index": index, "reason_code": str(exc)}, state)
            except Exception:
                rejected.append({"index": index, "reason_code": "E_INTERNAL_ENVELOPE"})
                emit_trace_event("feedback_rejected", {"index": index, "reason_code": "E_INTERNAL_ENVELOPE"}, state)

        if resolutions or status_updates or audits:
            self._repository.apply_feedback(
                scope,
                resolutions=list(resolutions.values()),
                status_updates=list(status_updates.values()),
                audit_records=audits,
            )
        emit_trace_event("feedback_applied", {"applied_count": len(delta), "rejected_count": len(rejected)}, state)
        return {
            "review_queue_delta": delta,
            "applied_count": len(delta),
            "rejected_decisions": rejected,
            "status": AgentStatus.SUCCESS.value,
        }


def _safe_note(raw: str, max_chars: int) -> str:
    normalized = unicodedata.normalize("NFKC", raw)
    if len(normalized) > max_chars:
        raise FeedbackRejectedError("E_NOTE_TOO_LONG")
    folded = normalized.casefold()
    if any(term in folded for term in _INJECTION_TERMS):
        raise FeedbackRejectedError("E_NOTE_INJECTION")
    findings = detect_pii(normalized)
    return mask_pii(normalized, findings) if findings else normalized


def _find_conflict(statuses: dict[str, dict[str, Any]], conflict_id: str):
    for facility_id, status in statuses.items():
        for conflict in status.get("conflicts", []):
            if conflict["conflict_id"] == conflict_id:
                return facility_id, status, conflict
    return None
