"""Reconcile observation duplicates/conflicts and persist one atomic batch."""

from __future__ import annotations

from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from framework.security import detect_pii
from framework.security.pii_masking import mask_pii
from shared.utils.audit_logger import emit_trace_event

from src.ingest_row_pipeline.checkpoint_scrubber import CheckpointScrubPolicy
from src.ingest_row_pipeline.row_pipeline import RowPipelineHooks
from src.nodes.inner.intake_extract import _review_item
from src.services.domain_utils import stable_id
from src.services.ingest_pipeline_adapter import DisasterIngestRowPipeline


DISASTER_CHECKPOINT_POLICY = CheckpointScrubPolicy(
    safe_keys=frozenset({"mode", "record_kind", "request_clock", "report_id", "row_index"}),
    scope_keys=frozenset({"disaster_event_id"}),
    enum_value_keys={"mode": frozenset({"ingest", "invoke", "feedback"}), "record_kind": frozenset({"damage_report"})},
    safe_scalar_list_keys=frozenset({"facility_id_snapshot"}),
)

_TRANSIENT_KEYS = {
    "extraction_item_id", "facility_mention", "facility_id", "category", "severity_observed",
    "access_blocked", "observed_at", "source_report_id", "quoted_span", "confidence",
    "evidence_digest",
}


class ReconcilePersistNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, *, repository=None) -> None:
        super().__init__()
        self._repository = repository

    def execute(self, state: dict) -> dict:
        if state.get("status") == AgentStatus.ERROR.value:
            emit_trace_event("status_persisted", {"written_count": 0, "upstream_error": True}, state)
            return {}
        if self._repository is None:
            emit_trace_event("status_persisted", {"written_count": 0, "error": "repository_unavailable"}, state)
            return {"status": AgentStatus.ERROR.value, "error_log": ["repository_error:RuntimeError"]}

        actual_written: list[str] = []
        generated_conflicts: list[dict[str, Any]] = []
        review = list(state.get("review_queue_delta", []))

        def sanitize(_state: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
            if set(row) != _TRANSIENT_KEYS:
                raise ValueError("E_SCHEMA_UNKNOWN_FIELD")
            span = row["quoted_span"]
            findings = detect_pii(span)
            if findings:
                row["quoted_span"] = mask_pii(span, findings)
            return row

        def normalize(_state: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
            observation_id = stable_id(
                row["facility_id"], row["category"], row["observed_at"], row["source_report_id"],
                row["severity_observed"], row["access_blocked"], row["quoted_span"],
            )
            return {
                "facility_id": row["facility_id"],
                "updated_at": state["request_clock"],
                "observation_id": observation_id,
                "category": row["category"],
                "severity_observed": row["severity_observed"],
                "access_blocked": row["access_blocked"],
                "observed_at": row["observed_at"],
                "source_report_id": row["source_report_id"],
                "quoted_span": row["quoted_span"],
                "confidence": row["confidence"],
                "evidence_digest": row["evidence_digest"],
            }

        def write_batch(_state: dict[str, Any], rows: list[dict[str, Any]]) -> list[str]:
            nonlocal generated_conflicts, review, actual_written
            existing = self._repository.load_status(state["scope"])
            by_facility = {item["facility_id"]: item for item in existing}
            conflict_map: dict[str, dict[str, Any]] = {}
            combined_prior: dict[str, list[dict[str, Any]]] = {
                facility_id: list(item["damage_observations"]) for facility_id, item in by_facility.items()
            }
            for row in rows:
                prior_rows = combined_prior.setdefault(row["facility_id"], [])
                for prior in prior_rows:
                    if (
                        prior["category"] == row["category"]
                        and prior["observed_at"] == row["observed_at"]
                        and (
                            prior["severity_observed"] != row["severity_observed"]
                            or prior["access_blocked"] != row["access_blocked"]
                        )
                    ):
                        conflict_id = stable_id(
                            state["scope"]["disaster_event_id"], row["facility_id"], row["category"], row["observed_at"]
                        )
                        current = conflict_map.setdefault(
                            conflict_id,
                            {
                                "facility_id": row["facility_id"],
                                "conflict_id": conflict_id,
                                "category": row["category"],
                                "observation_ids": [],
                                "note": "同一時点の観測値が矛盾しています。",
                                "detected_at": state["request_clock"],
                                "state": "open",
                            },
                        )
                        current["observation_ids"] = sorted(
                            set(current["observation_ids"] + [prior["observation_id"], row["observation_id"]])
                        )
                        review.append(
                            _review_item(state, row["source_report_id"], "R_CONFLICT", [])
                        )
                        emit_trace_event("conflict_detected", {"conflict_id": conflict_id}, state)
                prior_rows.append(row)
            generated_conflicts = list(conflict_map.values())
            actual_written = self._repository.apply_ingest(
                state["scope"],
                observations=rows,
                conflicts=generated_conflicts,
                review_items=_dedupe_review(review),
                audit_records=[],
            )
            return [row["observation_id"] for row in rows]

        pipeline = DisasterIngestRowPipeline(
            hooks=RowPipelineHooks(sanitize=sanitize, normalize=normalize, write_batch=write_batch),
            reject_batch_on_validation_error=True,
        )
        pipeline_state = {**state, "rows": list(state.get("resolved_observations", []))}
        part_result = pipeline.execute(pipeline_state)
        if not state.get("resolved_observations"):
            self._repository.apply_ingest(
                state["scope"], observations=[], conflicts=[], review_items=_dedupe_review(review), audit_records=[]
            )
        part_rejected = part_result.get("ingest_summary", {}).get("rejected", [])
        rejected = list(state.get("rejected", [])) + [
            {"row_index": item["index"], "reason_code": "E_SCHEMA_UNKNOWN_FIELD", "detail": ";".join(item["reasons"])}
            for item in part_rejected
        ]
        if part_rejected and state.get("resolved_observations"):
            # The vendored pipeline rejected the entire batch before write_batch.
            actual_written = []
        summary = {
            "written_ids": actual_written,
            "queued_ids": [item["queue_id"] for item in _dedupe_review(review)],
            "rejected_count": len(rejected),
        }
        degradations = list(state.get("degradation_reason", []))
        if state.get("report_refs") and not summary["written_ids"] and not summary["queued_ids"]:
            degradations.append("partial_ingest")
            emit_trace_event("degradation_recorded", {"reason_code": "partial_ingest"}, state)
        emit_trace_event("status_persisted", {"written_count": len(actual_written), "queued_count": len(summary["queued_ids"])}, state)
        return {
            "ingest_summary": summary,
            "review_queue_delta": _dedupe_review(review),
            "rejected": rejected,
            "degradation_reason": sorted(set(degradations)),
            "status": AgentStatus.SUCCESS.value,
        }


def _dedupe_review(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [dict(item) for _, item in sorted({item["queue_id"]: item for item in items}.items())]
