"""Resolve the armored inner envelope and select a mode."""

from __future__ import annotations

from typing import ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.envelope_validation import validate_internal_envelope


class DispatchNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, *, payload_store=None) -> None:
        super().__init__()
        self._payload_store = payload_store

    def execute(self, state: dict) -> dict:
        if state.get("status") == AgentStatus.ERROR.value:
            emit_trace_event("report_intake_started", {"mode": "error"}, state)
            return {}
        try:
            if self._payload_store is None:
                raise ValueError("payload store unavailable")
            envelope = self._payload_store.resolve_envelope(
                state.get("user_input"), session_id=state.get("session_id", ""), consume=True
            )
            envelope = validate_internal_envelope(envelope)
        except Exception:
            emit_trace_event("report_row_rejected", {"reason_code": "E_INTERNAL_ENVELOPE"}, state)
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["validation_error:ValueError:E_INTERNAL_ENVELOPE"],
            }
        mode = envelope["mode"]
        emit_trace_event("report_intake_started", {"mode": mode}, state)
        return {
            "request_mode": mode,
            "mode": mode,
            "scope": envelope["scope"],
            "request_clock": envelope["request_clock"],
            "report_refs": envelope["report_refs"],
            "facility_id_snapshot": envelope["facility_id_snapshot"],
            "as_of": envelope["as_of"],
            "decisions": envelope["decisions"],
            "caller_id": envelope["caller_id"],
            "rejected": envelope["rejected"],
            "degradation_reason": [],
            "status": AgentStatus.SUCCESS.value,
        }
