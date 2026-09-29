"""Resolve, quality-check, mask, and extract disaster observations."""

from __future__ import annotations

import json
import unicodedata
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from framework.security import detect_pii, evaluate_untrusted_content
from framework.security.pii_masking import mask_pii
from shared.utils.audit_logger import emit_trace_event

from src.ingest_file_sanitizer.file_sanitizer import TextQualityPolicy, assess_text_quality
from src.services.domain_utils import evidence_digest, stable_id
from src.llm_draft_stage.draft_node import parse_json_object


_CHANNELS = {"phone_transcript", "field_memo", "written_report", "photo_caption"}
_CATEGORIES = {"building", "utility", "access", "equipment", "other"}
_SEVERITIES = {"structural_damage", "partial_damage", "utility_outage", "no_visible_damage", "unknown"}
_OBS_KEYS = {
    "item_index", "facility_mention", "category", "severity_observed", "access_blocked",
    "observed_at", "quoted_span", "confidence",
}


class IntakeExtractNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, *, config: dict[str, Any] | None = None, payload_store=None, repository=None) -> None:
        super().__init__()
        self._config = dict(config or {})
        self._payload_store = payload_store
        self._repository = repository

    def execute(self, state: dict) -> dict:
        if state.get("status") == AgentStatus.ERROR.value:
            emit_trace_event("report_intake_started", {"mode": "error"}, state)
            return {}
        emit_trace_event("report_intake_started", {"report_count": len(state.get("report_refs", []))}, state)
        extracted: list[dict[str, Any]] = []
        rejected = list(state.get("rejected", []))
        review = list(state.get("review_queue_delta", []))
        degradations = list(state.get("degradation_reason", []))
        registry = self._repository.load_registry() if self._repository is not None else []
        llm = self._config.get("llm")
        if llm is None:
            degradations.append("llm_unavailable")
        quality = TextQualityPolicy(
            min_text_chars=1,
            min_printable_ratio=self._config.get("min_printable_ratio", 0.90),
            max_replacement_ratio=self._config.get("max_replacement_char_ratio", 0.02),
            max_control_ratio=self._config.get("max_control_char_ratio", 0.01),
            min_signal_ratio=0.15,
        )

        for ref in state.get("report_refs", []):
            row_index = ref["row_index"]
            try:
                raw = self._payload_store.resolve(
                    ref["payload_ref"], scope=state["scope"], session_id=state["session_id"], consume=True
                )
                if not isinstance(raw, dict) or set(raw) != {"report_id", "text", "reported_at", "channel"}:
                    raise ValueError("E_INTERNAL_ENVELOPE")
                if raw["report_id"] != ref["report_id"] or raw["channel"] not in _CHANNELS:
                    raise ValueError("E_ENUM_UNKNOWN")
                text = raw["text"]
                if not isinstance(text, str) or _binary_signature(text):
                    raise ValueError("E_BINARY_INPUT")
                normalized = unicodedata.normalize("NFKC", text)
                if len(normalized) > self._config.get("max_report_chars", 8000):
                    raise ValueError("E_TEXT_QUALITY")
                accepted, _reason = assess_text_quality(normalized, quality)
                if not accepted:
                    raise ValueError("E_TEXT_QUALITY")
                findings = detect_pii(normalized)
                masked = mask_pii(normalized, findings) if findings else normalized
                if findings:
                    emit_trace_event("pii_masked", {"row_index": row_index, "finding_count": len(findings)}, state)
                isolated = evaluate_untrusted_content(
                    masked,
                    source="damage_report",
                    state={**state, "status": AgentStatus.PENDING.value, "user_input": "", "validated_input": ""},
                    node_name=type(self).__name__,
                )
                if isolated.get("status") == AgentStatus.ERROR.value:
                    raise ValueError("E_INJECTION_SUSPECTED")
                observations = (
                    self._deterministic_observations(masked, raw, registry)
                    if llm is None
                    else self._llm_observations(llm, masked, raw)
                )
                validated, observation_rejections = self._validate_observations(observations, masked, raw, ref)
                rejected.extend(
                    {
                        "row_index": row_index,
                        "reason_code": reason_code,
                        "detail": f"observation {item_index} rejected",
                    }
                    for item_index, reason_code in observation_rejections
                )
                for item_index, reason_code in observation_rejections:
                    emit_trace_event(
                        "report_row_rejected",
                        {"row_index": row_index, "item_index": item_index, "reason_code": reason_code},
                        state,
                    )
                if not validated and llm is None:
                    review.append(_review_item(state, ref["report_id"], "R_LLM_UNAVAILABLE", []))
                extracted.extend(validated)
            except ValueError as exc:
                code = str(exc) if str(exc).startswith("E_") else "E_INTERNAL_ENVELOPE"
                rejected.append({"row_index": row_index, "reason_code": code, "detail": "report rejected"})
                emit_trace_event("report_row_rejected", {"row_index": row_index, "reason_code": code}, state)
            except Exception:
                review.append(_review_item(state, ref["report_id"], "R_LLM_CONTRACT", []))
                degradations.append("llm_contract_violation")
                emit_trace_event("entity_review_queued", {"reason_code": "R_LLM_CONTRACT"}, state)

        return {
            "extracted_observations": extracted,
            "rejected": rejected,
            "review_queue_delta": review,
            "degradation_reason": sorted(set(degradations)),
            "status": AgentStatus.SUCCESS.value,
        }

    def _deterministic_observations(
        self, masked: str, report: dict[str, Any], registry: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[tuple[int, str]]]:
        observations: list[dict[str, Any]] = []
        for facility in registry:
            mentions = [facility["name"], *facility.get("aliases", [])]
            mention = next((item for item in mentions if item and item in masked), None)
            if mention is None:
                continue
            start = max(0, masked.index(mention) - 20)
            end = min(len(masked), start + self._config.get("max_quoted_span_chars", 200))
            span = masked[start:end]
            observations.append(
                {
                    "item_index": len(observations),
                    "facility_mention": mention,
                    "category": "other",
                    "severity_observed": "unknown",
                    "access_blocked": False,
                    "observed_at": None,
                    "quoted_span": span,
                    "confidence": 1.0,
                }
            )
        return observations

    def _llm_observations(self, llm: Any, masked: str, report: dict[str, Any]) -> list[dict[str, Any]]:
        prompt = _build_intake_prompt(masked, report, self._config)
        response = llm.complete([{"role": "user", "content": prompt}])
        content = response.get("content", "") if isinstance(response, dict) else str(response)
        parsed = parse_json_object(content)
        if set(parsed) != {"observations"} or not isinstance(parsed["observations"], list):
            raise RuntimeError("LLM contract")
        return parsed["observations"]

    def _validate_observations(
        self,
        values: list[dict[str, Any]],
        masked: str,
        report: dict[str, Any],
        ref: dict[str, Any],
    ) -> list[dict[str, Any]]:
        if any(not isinstance(value, dict) or set(value) != _OBS_KEYS for value in values):
            raise RuntimeError("LLM contract")
        indexes = [value["item_index"] for value in values]
        if indexes != list(range(len(values))):
            raise RuntimeError("LLM contract")
        output: list[dict[str, Any]] = []
        rejected: list[tuple[int, str]] = []
        for value in values:
            item_index = value["item_index"]
            if value["category"] not in _CATEGORIES or value["severity_observed"] not in _SEVERITIES:
                rejected.append((item_index, "E_ENUM_UNKNOWN"))
                continue
            confidence = value["confidence"]
            span = value["quoted_span"]
            if not isinstance(span, str) or span not in masked:
                rejected.append((item_index, "E_SPAN_TOO_SHORT"))
                continue
            if len(span) < self._config.get("min_quoted_span_chars", 8):
                rejected.append((item_index, "E_SPAN_TOO_SHORT"))
                continue
            if len(span) > self._config.get("max_quoted_span_chars", 200):
                rejected.append((item_index, "E_SPAN_TOO_LONG"))
                continue
            if (
                isinstance(confidence, bool)
                or not isinstance(confidence, (int, float))
                or not 0 <= confidence <= 1
                or not isinstance(value["access_blocked"], bool)
            ):
                rejected.append((item_index, "E_RANGE"))
                continue
            output.append(
                {
                    "extraction_item_id": stable_id(report["report_id"], value["item_index"]),
                    "facility_mention": value["facility_mention"],
                    "category": value["category"],
                    "severity_observed": value["severity_observed"],
                    "access_blocked": value["access_blocked"],
                    "observed_at": value["observed_at"] or report["reported_at"],
                    "source_report_id": report["report_id"],
                    "quoted_span": span,
                    "confidence": float(confidence),
                    "evidence_digest": evidence_digest(span),
                }
            )
        return output, rejected


def _build_intake_prompt(masked: str, report: dict[str, Any], config: dict[str, Any]) -> str:
    minimum = config.get("min_quoted_span_chars", 8)
    maximum = config.get("max_quoted_span_chars", 200)
    return json.dumps(
        {
            "task": "Extract literal grounded facility damage observations. Return one JSON object only.",
            "schema": {
                "observations": [{
                    "item_index": "integer",
                    "facility_mention": "string",
                    "category": {"enum": sorted(_CATEGORIES)},
                    "severity_observed": {"enum": sorted(_SEVERITIES)},
                    "access_blocked": "boolean",
                    "observed_at": "RFC3339 string or null",
                    "quoted_span": "string",
                    "confidence": "number",
                }]
            },
            "constraints": {
                "item_index": "Use a zero-based contiguous sequence: 0, 1, 2, ... with no gaps or duplicates.",
                "quoted_span": (
                    "Copy a literal substring from masked_report for this observation; "
                    f"its length must be between {minimum} and {maximum} characters inclusive."
                ),
                "observed_at": "Return null when the time cannot be read literally from masked_report; never infer it.",
                "confidence": "Return a finite number from 0.0 through 1.0 inclusive.",
            },
            "reported_at": report["reported_at"],
            "masked_report": masked,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _binary_signature(value: str) -> bool:
    raw = value.encode("utf-8", errors="replace")
    return raw.startswith((b"%PDF", b"PK\x03\x04", b"\x89PNG", b"\xff\xd8\xff"))


def _review_item(state: dict, report_id: str, reason: str, candidates: list[dict[str, Any]]) -> dict[str, Any]:
    event_id = state["scope"]["disaster_event_id"]
    return {
        "queue_id": stable_id(event_id, report_id, reason),
        "disaster_event_id": event_id,
        "source_report_id": report_id,
        "reason_code": reason,
        "candidates": candidates,
        "reason_note": "",
        "state": "open",
        "created_at": state["request_clock"],
        "resolved_by": None,
        "resolved_at": None,
    }
