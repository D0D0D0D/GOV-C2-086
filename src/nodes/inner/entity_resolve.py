"""Observation-level deterministic candidate generation and grounded scoring."""

from __future__ import annotations

import json
import math
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.llm_draft_stage.draft_node import parse_json_object
from src.nodes.inner.intake_extract import _review_item
from src.services.domain_utils import bigrams, normalize_name


class EntityResolveNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, *, config: dict[str, Any] | None = None, repository=None) -> None:
        super().__init__()
        self._config = dict(config or {})
        self._repository = repository

    def execute(self, state: dict) -> dict:
        if state.get("status") == AgentStatus.ERROR.value:
            emit_trace_event("entity_review_queued", {"reason_code": "upstream_error"}, state)
            return {}
        registry = self._repository.load_registry() if self._repository is not None else []
        by_id = {row["facility_id"]: row for row in registry}
        llm = self._config.get("llm")
        resolved: list[dict[str, Any]] = []
        review = list(state.get("review_queue_delta", []))
        rejected = list(state.get("rejected", []))
        row_by_report = {
            item["report_id"]: item["row_index"] for item in state.get("report_refs", [])
        }
        degradations = list(state.get("degradation_reason", []))
        if not registry:
            degradations.append("facility_registry_empty")

        for item in state.get("extracted_observations", []):
            deterministic = self._exact_matches(item, registry)
            if len(deterministic) == 1:
                resolved.append({**item, "facility_id": deterministic[0]["facility_id"], "confidence": 1.0})
                emit_trace_event("entity_resolved", {"method": "deterministic"}, state)
                continue
            candidates = self._candidate_set(item, registry)
            public_candidates = [
                {"facility_id": row["facility_id"], "score": score, "quoted_span": item["quoted_span"]}
                for row, score in candidates
            ]
            if llm is None:
                review.append(_review_item(state, item["source_report_id"], "R_LLM_UNAVAILABLE", public_candidates))
                emit_trace_event("entity_review_queued", {"reason_code": "R_LLM_UNAVAILABLE"}, state)
                continue
            try:
                scored = self._score_with_llm(llm, item, candidates)
            except Exception:
                review.append(_review_item(state, item["source_report_id"], "R_LLM_CONTRACT", public_candidates))
                degradations.append("llm_contract_violation")
                emit_trace_event("entity_review_queued", {"reason_code": "R_LLM_CONTRACT"}, state)
                continue
            grounded, grounding_rejections = self._ground(
                item, scored, {row["facility_id"]: row for row, _ in candidates}
            )
            rejected.extend(
                {
                    "row_index": row_by_report.get(item["source_report_id"], -1),
                    "reason_code": reason_code,
                    "detail": f"candidate span rejected for {item['extraction_item_id']}",
                }
                for reason_code in grounding_rejections
            )
            if not grounded:
                review.append(_review_item(state, item["source_report_id"], "R_UNGROUNDED_CANDIDATE", public_candidates))
                emit_trace_event("entity_review_queued", {"reason_code": "R_UNGROUNDED_CANDIDATE"}, state)
                continue
            grounded.sort(key=lambda candidate: (-candidate["score"], candidate["facility_id"]))
            top = grounded[0]
            if top["score"] < self._config.get("resolution_confidence_threshold", 0.85):
                reason = "R_LOW_CONFIDENCE"
            elif len(grounded) > 1 and top["score"] - grounded[1]["score"] < self._config.get("resolution_margin", 0.15):
                reason = "R_AMBIGUOUS_CANDIDATES"
            else:
                reason = ""
            if reason:
                review.append(_review_item(state, item["source_report_id"], reason, grounded))
                emit_trace_event("entity_review_queued", {"reason_code": reason}, state)
                continue
            if top["facility_id"] not in by_id:
                review.append(_review_item(state, item["source_report_id"], "R_UNGROUNDED_CANDIDATE", grounded))
                continue
            resolved.append({**item, "facility_id": top["facility_id"], "confidence": top["score"]})
            emit_trace_event("entity_resolved", {"method": "candidate_scoring"}, state)

        emit_trace_event("entity_resolved", {"resolved_count": len(resolved), "queued_count": len(review)}, state)
        return {
            "resolved_observations": resolved,
            "review_queue_delta": _dedupe_review(review),
            "rejected": rejected,
            "degradation_reason": sorted(set(degradations)),
            "status": AgentStatus.SUCCESS.value,
        }

    @staticmethod
    def _exact_matches(item: dict[str, Any], registry: list[dict[str, Any]]) -> list[dict[str, Any]]:
        mention = normalize_name(str(item.get("facility_mention", "")))
        if not mention:
            return []
        return [
            row
            for row in registry
            if mention in {normalize_name(value) for value in [row["name"], *row.get("aliases", [])]}
        ]

    def _candidate_set(
        self, item: dict[str, Any], registry: list[dict[str, Any]]
    ) -> list[tuple[dict[str, Any], int]]:
        span_grams = bigrams(item["quoted_span"])
        minimum = self._config.get("candidate_min_bigram_hits", 2)
        candidates: list[tuple[dict[str, Any], int]] = []
        for row in registry:
            names = [value for value in [row["name"], *row.get("aliases", [])] if len(normalize_name(value)) >= 7]
            score = max((len(span_grams & bigrams(value)) for value in names), default=0)
            if score >= minimum:
                candidates.append((row, score))
        candidates.sort(key=lambda pair: (-pair[1], pair[0]["facility_id"]))
        return candidates[: self._config.get("candidate_top_k", 10)]

    @staticmethod
    def _score_with_llm(llm: Any, item: dict[str, Any], candidates: list[tuple[dict[str, Any], int]]) -> list[dict[str, Any]]:
        prompt = _build_entity_resolution_prompt(item, candidates)
        response = llm.complete([{"role": "user", "content": prompt}])
        content = response.get("content", "") if isinstance(response, dict) else str(response)
        parsed = parse_json_object(content)
        if set(parsed) != {"resolutions"} or not isinstance(parsed["resolutions"], list) or len(parsed["resolutions"]) != 1:
            raise ValueError("E_LLM_CONTRACT")
        resolution = parsed["resolutions"][0]
        if set(resolution) != {"extraction_item_id", "candidates"} or resolution["extraction_item_id"] != item["extraction_item_id"]:
            raise ValueError("E_LLM_CONTRACT")
        if not isinstance(resolution["candidates"], list):
            raise ValueError("E_LLM_CONTRACT")
        return resolution["candidates"]

    def _ground(
        self,
        item: dict[str, Any],
        values: list[dict[str, Any]],
        candidate_by_id: dict[str, dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[str]]:
        result: dict[str, dict[str, Any]] = {}
        rejected: list[str] = []
        for value in values:
            if not isinstance(value, dict) or set(value) != {"facility_id", "score", "quoted_span"}:
                continue
            facility = candidate_by_id.get(value["facility_id"])
            score = value["score"]
            span = value["quoted_span"]
            if (
                facility is None
                or isinstance(score, bool)
                or not isinstance(score, (int, float))
                or not math.isfinite(float(score))
                or not 0 <= float(score) <= 1
            ):
                continue
            if not isinstance(span, str) or len(span) < self._config.get("min_quoted_span_chars", 8):
                rejected.append("E_SPAN_TOO_SHORT")
                continue
            if len(span) > self._config.get("max_quoted_span_chars", 200):
                rejected.append("E_SPAN_TOO_LONG")
                continue
            if span != item["quoted_span"]:
                continue
            normalized_span = normalize_name(span)
            if not any(normalize_name(name) in normalized_span for name in [facility["name"], *facility.get("aliases", [])]):
                continue
            candidate = {"facility_id": facility["facility_id"], "score": float(score), "quoted_span": span}
            prior = result.get(candidate["facility_id"])
            if prior is None or candidate["score"] > prior["score"]:
                result[candidate["facility_id"]] = candidate
        return list(result.values()), sorted(set(rejected))


def _dedupe_review(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [dict(item) for _, item in sorted({item["queue_id"]: item for item in items}.items())]


def _build_entity_resolution_prompt(
    item: dict[str, Any], candidates: list[tuple[dict[str, Any], int]]
) -> str:
    public_candidates = [
        {"facility_id": row["facility_id"], "name": row["name"], "aliases": row.get("aliases", [])}
        for row, _ in candidates
    ]
    return json.dumps(
        {
            "task": "Score only the supplied facility candidates for this single observation. Return one JSON object only.",
            "schema": {
                "resolutions": [{
                    "extraction_item_id": "string",
                    "candidates": [{"facility_id": "string", "score": "number", "quoted_span": "string"}],
                }]
            },
            "constraints": {
                "resolutions": "Return exactly one resolution object.",
                "extraction_item_id": f"Return exactly {item['extraction_item_id']!r}.",
                "facility_id": "Use only a facility_id from allowed_facility_ids; never invent a facility.",
                "score": "Return a finite number from 0.0 through 1.0 inclusive.",
                "quoted_span": "Return quoted_span exactly as supplied for this observation.",
                "grounding": "Score a facility only when its name or alias occurs literally in this same quoted_span.",
            },
            "extraction_item_id": item["extraction_item_id"],
            "quoted_span": item["quoted_span"],
            "allowed_facility_ids": [candidate["facility_id"] for candidate in public_candidates],
            "candidates": public_candidates,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
