"""Fail-closed validation for server-built internal envelopes."""

from __future__ import annotations

from datetime import datetime
from typing import Any


INTERNAL_KEYS = {
    "mode", "scope", "request_clock", "caller_id", "report_refs", "facility_id_snapshot", "as_of",
    "decisions", "record_kind", "rejected",
}
_MODES = {"ingest", "invoke", "feedback"}
_CHANNELS = {"phone_transcript", "field_memo", "written_report", "photo_caption"}
_ACTIONS = {"accept_candidate", "reject_all", "assign_facility", "resolve_conflict"}


def validate_internal_envelope(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != INTERNAL_KEYS:
        raise ValueError("E_INTERNAL_ENVELOPE")
    mode = value.get("mode")
    if mode not in _MODES:
        raise ValueError("E_MODE_UNKNOWN")
    scope = value.get("scope")
    if not isinstance(scope, dict) or set(scope) != {"disaster_event_id"} or not _clean(scope["disaster_event_id"]):
        raise ValueError("E_SCOPE_REQUIRED")
    if not _timestamp(value.get("request_clock")) or not _timestamp(value.get("as_of")):
        raise ValueError("E_INTERNAL_ENVELOPE")
    if not _clean(value.get("caller_id")):
        raise ValueError("E_INTERNAL_ENVELOPE")
    if not isinstance(value.get("rejected"), list):
        raise ValueError("E_INTERNAL_ENVELOPE")
    refs = value.get("report_refs")
    facilities = value.get("facility_id_snapshot")
    decisions = value.get("decisions")
    if not isinstance(refs, list) or not isinstance(facilities, list) or not isinstance(decisions, list):
        raise ValueError("E_INTERNAL_ENVELOPE")
    for ref in refs:
        if (
            not isinstance(ref, dict)
            or set(ref) != {"row_index", "report_id", "payload_ref"}
            or isinstance(ref["row_index"], bool)
            or not isinstance(ref["row_index"], int)
            or ref["row_index"] < 0
            or not _identifier(ref["report_id"])
            or not _reference(ref["payload_ref"])
        ):
            raise ValueError("E_INTERNAL_ENVELOPE")
    if any(not _identifier(item) for item in facilities) or facilities != sorted(set(facilities)):
        raise ValueError("E_INTERNAL_ENVELOPE")
    for decision in decisions:
        _validate_decision(decision)
    if mode == "ingest":
        if value.get("record_kind") != "damage_report" or not refs or facilities or decisions:
            raise ValueError("E_INTERNAL_ENVELOPE")
    elif mode == "invoke":
        if value.get("record_kind") is not None or refs or decisions:
            raise ValueError("E_INTERNAL_ENVELOPE")
    else:
        if value.get("record_kind") is not None or refs or facilities or not decisions:
            raise ValueError("E_INTERNAL_ENVELOPE")
    return value


def _validate_decision(value: Any) -> None:
    if not isinstance(value, dict):
        raise ValueError("E_INTERNAL_ENVELOPE")
    allowed = {"queue_id", "conflict_id", "action", "facility_id", "keep_observation_ids", "note_ref"}
    if set(value) - allowed or value.get("action") not in _ACTIONS:
        raise ValueError("E_INTERNAL_ENVELOPE")
    queue_id = value.get("queue_id")
    conflict_id = value.get("conflict_id")
    if bool(queue_id) == bool(conflict_id) or (queue_id and not _identifier(queue_id)) or (conflict_id and not _identifier(conflict_id)):
        raise ValueError("E_DECISION_TARGET")
    requires_facility = value["action"] in {"accept_candidate", "assign_facility"}
    if requires_facility != ("facility_id" in value) or (requires_facility and not _identifier(value["facility_id"])):
        raise ValueError("E_DECISION_FIELD")
    requires_keep = value["action"] == "resolve_conflict"
    if requires_keep != ("keep_observation_ids" in value):
        raise ValueError("E_DECISION_FIELD")
    if requires_keep and (
        not isinstance(value["keep_observation_ids"], list)
        or not value["keep_observation_ids"]
        or any(not _identifier(item) for item in value["keep_observation_ids"])
    ):
        raise ValueError("E_DECISION_FIELD")
    if "note_ref" in value and value["note_ref"] is not None and not _reference(value["note_ref"]):
        raise ValueError("E_INTERNAL_ENVELOPE")


def _clean(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value == value.strip() and "\n" not in value and "\r" not in value


def _identifier(value: Any) -> bool:
    return _clean(value) and len(value) <= 128


def _reference(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 32 and set(value) <= set("abcdefghijklmnop")


def _timestamp(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None
