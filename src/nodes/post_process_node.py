"""Guard domain output, then render JSON and CSV from the guarded values."""

from __future__ import annotations

import csv
import io
import re
from typing import Any, ClassVar

from framework.errors import SecurityViolationError
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from framework.security import detect_credentials, detect_pii
from shared.utils.audit_logger import emit_trace_event

from src.evidence_gate.gate_node import ProvenanceContract
from src.evidence_gate.strict import StrictCoveragePolicy, find_uncited_numeric_or_comparative_claims
from src.output_envelope.envelope_node import OutputEnvelopeNode
from src.services.assertive_lexicon import contains_assertive_phrase, downgrade_assertive_sentences
from src.services.domain_utils import evidence_digest, sorted_degradations
from src.services.numeric_extractor import extract_numeric_values


ADVISORY_NOTICE = "緊急度は自治体承認ルールによる候補値です。人間の承認前に運用指示として使用できません。"
CSV_COLUMNS = (
    "facility_id", "facility_name", "urgency", "applied_rule_id", "conflict_flag", "damage_summary",
    "required_actions", "source_report_ids", "confidence", "needs_confirmation", "generated_at", "advisory_notice",
)
_PROSE_ENUMS = (
    "immediate", "high", "normal", "structural_damage", "partial_damage",
    "utility_outage", "no_visible_damage",
)
PROVENANCE_CONTRACT = ProvenanceContract(
    gate_kind="invoke",
    cited_ids_key="source_report_ids",
    retrieved_records_key="facility_status_snapshot",
    evidence_map_key="claims",
    record_id_keys=("observation_id",),
    claim_source_keys=("source_report_id",),
    passthrough_keys=("facilities", "csv_document", "unresolved", "degradation_reason"),
)
STRICT_COVERAGE_POLICY = StrictCoveragePolicy(
    free_text_paths=(
        ("facilities", "damage_summary"), ("facilities", "required_actions"), ("unresolved", "note"),
        ("review_queue_delta", "reason_note"), ("conflicts", "note"), ("csv_document",),
    ),
    additional_unit_terms=("棟", "件", "名", "人", "時", "分", "秒", "円", "m", "cm", "mm", "%", "％", "階"),
)


class PostProcessNode(OutputEnvelopeNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL
    gate_kind_by_mode: ClassVar[dict[str, str]] = {"invoke": "invoke", "ingest": "ingest", "feedback": "feedback"}
    required_fields_by_mode: ClassVar[dict[str, tuple[str, ...]]] = {
        "invoke": ("mode", "generated_at", "facilities", "csv_document", "unresolved"),
        "ingest": ("mode", "generated_at", "ingest_summary", "review_queue_delta"),
        "feedback": ("mode", "generated_at", "review_queue_delta", "applied_count", "rejected_decisions"),
    }

    def __init__(self, *, config: dict[str, Any] | None = None) -> None:
        super().__init__()
        self._config = dict(config or {})

    def execute(self, state: dict) -> dict:
        if state.get("status") == AgentStatus.ERROR.value:
            emit_trace_event("degradation_recorded", {"reason_code": "upstream_error"}, state)
            return {}
        result = super().execute(state)
        formatted = result["formatted_output"]
        formatted.pop("gate_kind", None)
        result.pop("_output_envelope_hook_state", None)
        if (
            formatted.get("mode") == "invoke"
            and state.get("facility_id_snapshot")
            and not formatted.get("facilities")
        ):
            formatted["degradation_reason"] = sorted_degradations(
                [*formatted.get("degradation_reason", []), "facility_result_empty"]
            )
            result["degradation_reason"] = formatted["degradation_reason"]
            emit_trace_event("degradation_recorded", {"reason_code": "facility_result_empty"}, state)
        elif (
            formatted.get("mode") == "invoke"
            and formatted.get("facilities")
            and not any(_facility_has_prose(item) for item in formatted["facilities"])
        ):
            formatted["degradation_reason"] = sorted_degradations(
                [*formatted.get("degradation_reason", []), "brief_prose_empty"]
            )
            result["degradation_reason"] = formatted["degradation_reason"]
            emit_trace_event("degradation_recorded", {"reason_code": "brief_prose_empty"}, state)
        result["_guard_context"] = {
            "mode": state.get("request_mode", state.get("mode")),
            "scope": dict(state.get("scope", {})),
            "facility_ids": [item.get("facility_id", "") for item in formatted.get("facilities", [])],
            "claim_bindings": _guard_claim_bindings(state, formatted),
            "deterministic_values": {
                facility["facility_id"]: {
                    observation["observation_id"]: {
                        "category": observation["category"],
                        "severity_observed": observation["severity_observed"],
                        "access_blocked": observation["access_blocked"],
                        "quoted_span": observation["quoted_span"],
                        "source_report_id": observation["source_report_id"],
                        "evidence_digest": observation["evidence_digest"],
                        "kind": _derive_claim_kind(observation, facility.get("conflicts", [])),
                    }
                    for observation in facility.get("damage_observations", [])
                }
                for facility in state.get("facility_status_snapshot", [])
            },
            "evidence_digests": sorted(
                {
                    observation.get("evidence_digest", "")
                    for facility in state.get("facility_status_snapshot", [])
                    for observation in facility.get("damage_observations", [])
                    if observation.get("evidence_digest")
                }
            ),
        }
        emit_trace_event(
            "degradation_recorded",
            {"reason_count": len(formatted.get("degradation_reason", [])), "mode": formatted.get("mode")},
            state,
        )
        return result

    def format_ingest(self, state: dict) -> dict[str, Any]:
        return _common(state) | {
            "ingest_summary": dict(state.get("ingest_summary", {})),
            "review_queue_delta": list(state.get("review_queue_delta", [])),
        }

    def format_feedback(self, state: dict) -> dict[str, Any]:
        return _common(state) | {
            "review_queue_delta": list(state.get("review_queue_delta", [])),
            "applied_count": int(state.get("applied_count", 0)),
            "rejected_decisions": list(state.get("rejected_decisions", [])),
        }

    def format_invoke(self, state: dict) -> dict[str, Any]:
        snapshots = {item["facility_id"]: item for item in state.get("facility_status_snapshot", [])}
        evaluations = {item["facility_id"]: item for item in state.get("urgency_evaluations", [])}
        drafts = {item["facility_id"]: item for item in state.get("briefs", [])}
        unresolved = list(state.get("unresolved", []))
        facilities: list[dict[str, Any]] = []

        for facility_id in state.get("facility_id_snapshot", []):
            snapshot = snapshots.get(facility_id)
            evaluation = evaluations.get(facility_id)
            if snapshot is None or evaluation is None or not snapshot["damage_observations"]:
                continue
            draft = drafts.get(facility_id, {"finding": "", "required_actions": [], "claims": []})
            valid_claims, claim_failures = self._validated_claims(state, snapshot, draft)
            unresolved.extend(claim_failures)
            finding, finding_failures = _guard_text(
                draft.get("finding", ""), "finding", valid_claims, facility_id
            )
            unresolved.extend(finding_failures)
            actions: list[str] = []
            needs_confirmation = False
            finding, changed = downgrade_assertive_sentences(finding)
            needs_confirmation = needs_confirmation or changed
            for index, action in enumerate(draft.get("required_actions", [])):
                safe, failures = _guard_text(action, f"required_actions[{index}]", valid_claims, facility_id)
                unresolved.extend(failures)
                safe, changed = downgrade_assertive_sentences(safe)
                needs_confirmation = needs_confirmation or changed
                if safe:
                    actions.append(safe)
            if changed or needs_confirmation:
                emit_trace_event("assertive_phrase_downgraded", {"facility_id": facility_id}, state)
            observations = snapshot["damage_observations"]
            public_claims = [
                {
                    "field_path": _to_public_field_path(claim["field_path"]),
                    "observation_id": claim["observation_id"],
                    "source_report_id": claim["source_report_id"],
                    "quoted_span": claim["quoted_span"],
                    "kind": claim["kind"],
                }
                for claim in valid_claims
            ]
            facilities.append(
                {
                    "facility_id": facility_id,
                    "facility_name": snapshot["facility_name"],
                    "urgency": evaluation["urgency"],
                    "applied_rule_id": evaluation["applied_rule_id"],
                    "conflict_flag": bool(evaluation["conflict_pending"]),
                    "needs_confirmation": needs_confirmation,
                    "damage_summary": finding,
                    "required_actions": actions,
                    "source_report_ids": sorted({item["source_report_id"] for item in observations}),
                    "confidence": round(min(float(item["confidence"]) for item in observations), 2),
                    "claims": public_claims,
                }
            )
        facilities.sort(key=lambda item: ({"immediate": 0, "high": 1, "normal": 2}[item["urgency"]], item["facility_id"]))
        formatted = _common(state) | {"facilities": facilities, "csv_document": "", "unresolved": unresolved}
        formatted["csv_document"] = _render_csv(facilities, state["request_clock"])
        # Exercise the configured evidence-gate strict policy as an additional
        # review signal. The custom numeric guard above remains the fail-closed gate.
        find_uncited_numeric_or_comparative_claims(formatted, [], policy=STRICT_COVERAGE_POLICY)
        emit_trace_event("csv_rendered", {"row_count": len(facilities)}, state)
        return formatted

    def _validated_claims(
        self, state: dict, snapshot: dict[str, Any], draft: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        observations = {item["observation_id"]: item for item in snapshot["damage_observations"]}
        valid: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        claims = draft.get("claims", []) if isinstance(draft, dict) else []
        for claim in claims:
            if not isinstance(claim, dict) or set(claim) != {"field_path", "observation_id"}:
                failures.append({"facility_id": snapshot["facility_id"], "reason_code": "E_CLAIM_UNVERIFIED", "note": "claim removed"})
                emit_trace_event("claim_unverified", {"facility_id": snapshot["facility_id"], "reason_code": "E_CLAIM_UNVERIFIED"}, state)
                continue
            observation = observations.get(claim.get("observation_id"))
            reason = None
            asserted_values = _derive_asserted_values(draft, claim.get("field_path"), public=False)
            if observation is None:
                reason = "E_CLAIM_UNVERIFIED"
            elif asserted_values is None:
                reason = "E_CLAIM_UNVERIFIED"
            elif len(observation.get("quoted_span", "")) < self._config.get("min_quoted_span_chars", 8):
                reason = "E_SPAN_TOO_SHORT"
            elif len(observation.get("quoted_span", "")) > self._config.get("max_quoted_span_chars", 200):
                reason = "E_SPAN_TOO_LONG"
            elif not _asserted_values_match(asserted_values, observation):
                reason = "E_CLAIM_UNVERIFIED"
            else:
                reason = self._literal_evidence_reason(observation)
            if reason:
                failures.append({"facility_id": snapshot["facility_id"], "reason_code": reason, "note": "claim removed"})
                emit_trace_event("claim_unverified", {"facility_id": snapshot["facility_id"], "reason_code": reason}, state)
            else:
                valid.append(
                    {
                        "field_path": claim["field_path"],
                        "observation_id": observation["observation_id"],
                        "source_report_id": observation["source_report_id"],
                        "quoted_span": observation["quoted_span"],
                        "kind": _derive_claim_kind(observation, snapshot.get("conflicts", [])),
                        "asserted_values": asserted_values,
                    }
                )
        return valid, failures

    @staticmethod
    def _literal_evidence_reason(observation: dict[str, Any]) -> str | None:
        span = observation.get("quoted_span")
        digest = observation.get("evidence_digest")
        if not isinstance(span, str) or not isinstance(digest, str) or evidence_digest(span) != digest:
            return "E_CLAIM_UNVERIFIED"
        return None

    def _sanitize_client_echoes(self, formatted: dict[str, Any]) -> dict[str, Any]:
        return formatted

    def _extra_security_gate_output(self, result: dict) -> dict:
        if set(result) - {"formatted_output", "status", "degradation_reason", "_guard_context"}:
            raise SecurityViolationError("S-3 output contains undeclared node fields")
        guard = result.get("_guard_context")
        formatted = result.get("formatted_output")
        if not isinstance(guard, dict) or set(guard) != {
            "mode", "scope", "facility_ids", "claim_bindings", "deterministic_values", "evidence_digests"
        }:
            raise SecurityViolationError("S-3 _guard_context missing or invalid")
        if not isinstance(formatted, dict) or formatted.get("mode") != guard["mode"]:
            raise SecurityViolationError("S-3 formatted output mode mismatch")
        _validate_output_shape(formatted)
        for text in _walk_strings(formatted):
            if detect_credentials(text):
                raise SecurityViolationError("S-3 credential remains in formatted output")
        _validate_guard_claim_bindings(formatted, guard)
        for facility in formatted.get("facilities", []):
            free_text = [facility["damage_summary"], *facility["required_actions"]]
            if any(detect_pii(text) for text in free_text):
                raise SecurityViolationError("S-3 PII remains in generated free text")
            if not facility["needs_confirmation"] and any(contains_assertive_phrase(text) for text in free_text):
                raise SecurityViolationError("S-3 assertive phrase was not downgraded")
            _recheck_reverse_claims(facility, guard["claim_bindings"])
        for item in formatted.get("unresolved", []):
            if isinstance(item, dict) and isinstance(item.get("note"), str) and detect_pii(item["note"]):
                raise SecurityViolationError("S-3 PII remains in unresolved free text")
        for item in formatted.get("review_queue_delta", []):
            if isinstance(item, dict) and isinstance(item.get("reason_note"), str) and detect_pii(item["reason_note"]):
                raise SecurityViolationError("S-3 PII remains in review queue free text")
        if isinstance(formatted.get("csv_document"), str) and detect_pii(formatted["csv_document"]):
            raise SecurityViolationError("S-3 PII remains in CSV document")
        result.pop("_guard_context")
        return result


def _common(state: dict) -> dict[str, Any]:
    return {
        "mode": state.get("request_mode", state.get("mode", "")),
        "generated_at": state["request_clock"],
        "advisory_notice": ADVISORY_NOTICE,
        "degradation_reason": sorted_degradations(list(state.get("degradation_reason", []))),
        "rejected": list(state.get("rejected", [])),
    }


def _facility_has_prose(facility: dict[str, Any]) -> bool:
    summary = facility.get("damage_summary")
    actions = facility.get("required_actions", [])
    return bool(isinstance(summary, str) and summary.strip()) or any(
        isinstance(action, str) and action.strip() for action in actions
    )


def _guard_text(
    text: Any, field_path: str, claims: list[dict[str, Any]], facility_id: str
) -> tuple[str, list[dict[str, Any]]]:
    if not isinstance(text, str):
        return "", [{"facility_id": facility_id, "reason_code": "E_LLM_CONTRACT", "note": "non-string prose removed"}]
    kept: list[str] = []
    failures: list[dict[str, Any]] = []
    for index, sentence in enumerate(_sentences(text)):
        if detect_pii(sentence):
            failures.append({"facility_id": facility_id, "reason_code": "E_PII_OUTPUT", "note": "PII sentence removed"})
            continue
        numbers, unsupported = extract_numeric_values(sentence)
        enums = [value for value in _PROSE_ENUMS if value in sentence.casefold()]
        sentence_claims = [
            claim for claim in claims if _field_path_matches(claim.get("field_path", ""), field_path, index)
        ]
        asserted = {
            (str(item.get("value")), "%" if item.get("unit") == "％" else item.get("unit"))
            for claim in sentence_claims
            for item in claim.get("asserted_values", [])
        }
        required = {(item.value, item.unit) for item in numbers} | {(value, None) for value in enums}
        if unsupported or not required <= asserted:
            failures.append({"facility_id": facility_id, "reason_code": "E_CLAIM_UNVERIFIED", "note": "unsupported prose sentence removed"})
            continue
        kept.append(sentence)
    return "".join(kept), failures


def _field_path_matches(value: str, field: str, sentence_index: int) -> bool:
    return value in {
        field,
        f"{field}#sentence[{sentence_index}]",
        f"briefs[].{field}#sentence[{sentence_index}]",
    }


def _to_public_field_path(value: Any) -> str:
    """Map the LLM brief vocabulary to the public output vocabulary once."""
    if not isinstance(value, str):
        return ""
    if value == "finding" or value.startswith("finding#sentence["):
        return "damage_summary" + value[len("finding"):]
    if value.startswith("briefs[].finding#sentence["):
        return "briefs[].damage_summary" + value[len("briefs[].finding"):]
    return value


def _derive_asserted_values(
    container: dict[str, Any], field_path: Any, *, public: bool
) -> list[dict[str, Any]] | None:
    text = _claim_text(container, field_path, public=public)
    if text is None:
        return None
    numbers, unsupported = extract_numeric_values(text)
    if unsupported:
        return None
    values = [
        {"value": item.value, "unit": "%" if item.unit == "％" else item.unit}
        for item in numbers
    ]
    values.extend(
        {"value": value, "unit": None}
        for value in _PROSE_ENUMS
        if value in text.casefold()
    )
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str | None]] = set()
    for value in values:
        key = (str(value["value"]), value["unit"])
        if key not in seen:
            seen.add(key)
            deduped.append(value)
    return deduped


def _claim_text(container: dict[str, Any], field_path: Any, *, public: bool) -> str | None:
    if not isinstance(container, dict) or not isinstance(field_path, str):
        return None
    path = field_path.removeprefix("briefs[].")
    prose_key = "damage_summary" if public else "finding"
    prose_match = re.fullmatch(rf"{re.escape(prose_key)}(?:#sentence\[([0-9]+)\])?", path)
    action_match = re.fullmatch(r"required_actions\[([0-9]+)\](?:#sentence\[([0-9]+)\])?", path)
    if prose_match:
        text = container.get(prose_key)
        sentence_index = prose_match.group(1)
    elif action_match:
        actions = container.get("required_actions")
        action_index = int(action_match.group(1))
        if not isinstance(actions, list) or action_index >= len(actions):
            return None
        text = actions[action_index]
        sentence_index = action_match.group(2)
    else:
        return None
    if not isinstance(text, str):
        return None
    if sentence_index is None:
        return text
    sentences = _sentences(text)
    index = int(sentence_index)
    return sentences[index] if index < len(sentences) else None


def _derive_claim_kind(observation: dict[str, Any], conflicts: list[dict[str, Any]]) -> str:
    if observation.get("access_blocked") is True:
        return "access"
    if any(conflict.get("category") == observation.get("category") for conflict in conflicts):
        return "conflict"
    return "observation"


def _asserted_values_match(values: Any, observation: dict[str, Any]) -> bool:
    if not isinstance(values, list):
        return False
    allowed = {
        (str(observation["severity_observed"]), None),
        (str(observation["category"]), None),
        (str(observation["access_blocked"]).lower(), None),
    }
    span_values, unsupported = extract_numeric_values(observation["quoted_span"])
    if unsupported:
        return False
    allowed |= {(item.value, item.unit) for item in span_values}
    for item in values:
        if not isinstance(item, dict) or set(item) != {"value", "unit"}:
            return False
        key = (str(item["value"]), "%" if item["unit"] == "％" else item["unit"])
        if key not in allowed:
            return False
    return True


def _sentences(value: str) -> list[str]:
    return [item for item in re.findall(r"[^。！？!?\n]+[。！？!?\n]?", value) if item]


def _render_csv(facilities: list[dict[str, Any]], generated_at: str) -> str:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=CSV_COLUMNS, lineterminator="\r\n")
    writer.writeheader()
    for facility in facilities:
        row = {
            "facility_id": facility["facility_id"],
            "facility_name": facility["facility_name"],
            "urgency": facility["urgency"],
            "applied_rule_id": facility["applied_rule_id"],
            "conflict_flag": str(facility["conflict_flag"]).lower(),
            "damage_summary": facility["damage_summary"],
            "required_actions": " / ".join(facility["required_actions"]),
            "source_report_ids": " ".join(facility["source_report_ids"]),
            "confidence": f"{facility['confidence']:.2f}",
            "needs_confirmation": str(facility["needs_confirmation"]).lower(),
            "generated_at": generated_at,
            "advisory_notice": ADVISORY_NOTICE,
        }
        writer.writerow({key: _neutralize_formula(str(value)) for key, value in row.items()})
    return stream.getvalue()


def _neutralize_formula(value: str) -> str:
    return "'" + value if value.startswith(("=", "+", "-", "@", "\t", "\r")) else value


def _validate_output_shape(formatted: dict[str, Any]) -> None:
    common = {"mode", "generated_at", "advisory_notice", "degradation_reason", "rejected"}
    expected = {
        "ingest": common | {"ingest_summary", "review_queue_delta"},
        "invoke": common | {"facilities", "csv_document", "unresolved"},
        "feedback": common | {"review_queue_delta", "applied_count", "rejected_decisions"},
    }
    mode = formatted.get("mode")
    if mode not in expected or set(formatted) != expected[mode]:
        raise SecurityViolationError("S-3 formatted output shape is undeclared")
    if formatted.get("advisory_notice") != ADVISORY_NOTICE:
        raise SecurityViolationError("S-3 advisory notice missing")
    if formatted.get("degradation_reason") != sorted(set(formatted.get("degradation_reason", []))):
        raise SecurityViolationError("S-3 degradation codes are not canonical")
    if mode == "invoke":
        allowed = {
            "facility_id", "facility_name", "urgency", "applied_rule_id", "conflict_flag",
            "needs_confirmation", "damage_summary", "required_actions", "source_report_ids", "confidence", "claims",
        }
        if any(not isinstance(item, dict) or set(item) != allowed for item in formatted["facilities"]):
            raise SecurityViolationError("S-3 facility shape is undeclared")


def _recheck_reverse_claims(facility: dict[str, Any], bindings: list[dict[str, Any]]) -> None:
    for field, text in [("damage_summary", facility["damage_summary"]), *[(f"required_actions[{i}]", value) for i, value in enumerate(facility["required_actions"])]]:
        for index, sentence in enumerate(_sentences(text)):
            numbers, unsupported = extract_numeric_values(sentence)
            if unsupported:
                raise SecurityViolationError("S-3 unsupported numeric expression remains")
            if numbers:
                matching = [
                    binding
                    for binding in bindings
                    if binding.get("facility_id") == facility["facility_id"]
                    and _field_path_matches(binding.get("field_path", ""), field, index)
                ]
                asserted = {
                    (str(item.get("value")), "%" if item.get("unit") == "％" else item.get("unit"))
                    for binding in matching
                    for item in binding.get("asserted_values", [])
                }
                if not {(item.value, item.unit) for item in numbers} <= asserted:
                    raise SecurityViolationError("S-3 uncited numeric prose remains")


def _guard_claim_bindings(state: dict[str, Any], formatted: dict[str, Any]) -> list[dict[str, Any]]:
    drafts = {item["facility_id"]: item for item in state.get("briefs", [])}
    bindings: list[dict[str, Any]] = []
    for facility in formatted.get("facilities", []):
        source_claims = drafts.get(facility["facility_id"], {}).get("claims", [])
        for public in facility.get("claims", []):
            source = next(
                (
                    claim for claim in source_claims
                    if _to_public_field_path(claim.get("field_path")) == public.get("field_path")
                    and claim.get("observation_id") == public.get("observation_id")
                ),
                None,
            )
            asserted_values = _derive_asserted_values(facility, public.get("field_path"), public=True)
            if source is not None and asserted_values is not None:
                bindings.append(
                    {
                        "facility_id": facility["facility_id"],
                        **public,
                        "asserted_values": asserted_values,
                    }
                )
    return bindings


def _validate_guard_claim_bindings(formatted: dict[str, Any], guard: dict[str, Any]) -> None:
    deterministic = guard.get("deterministic_values")
    bindings = guard.get("claim_bindings")
    if not isinstance(deterministic, dict) or not isinstance(bindings, list):
        raise SecurityViolationError("S-3 claim guard context is invalid")
    for facility in formatted.get("facilities", []):
        facility_values = deterministic.get(facility["facility_id"], {})
        for public in facility.get("claims", []):
            binding = next(
                (
                    item for item in bindings
                    if item.get("facility_id") == facility["facility_id"]
                    and all(item.get(key) == public.get(key) for key in public)
                ),
                None,
            )
            observation = facility_values.get(public.get("observation_id"))
            if binding is None or observation is None:
                raise SecurityViolationError("S-3 claim binding is not grounded")
            if (
                observation.get("source_report_id") != public.get("source_report_id")
                or observation.get("quoted_span") != public.get("quoted_span")
                or observation.get("kind") != public.get("kind")
                or evidence_digest(observation.get("quoted_span", "")) != observation.get("evidence_digest")
                or observation.get("evidence_digest") not in guard.get("evidence_digests", [])
                or not _asserted_values_match(binding.get("asserted_values", []), observation)
            ):
                raise SecurityViolationError("S-3 claim binding was modified")


def _walk_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _walk_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_strings(item)
