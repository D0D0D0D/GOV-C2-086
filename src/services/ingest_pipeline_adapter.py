"""Configured use of the vendored fail-closed ingest row pipeline."""

from typing import ClassVar

from framework.schemas.trust_level import TrustLevel

from src.ingest_row_pipeline.row_pipeline import IngestRowPipelineNode


class DisasterIngestRowPipeline(IngestRowPipelineNode):
    """Only the part's declared required-field adaptation for this domain."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL
    required_fields: ClassVar[tuple[str, ...]] = (
        "facility_id", "category", "severity_observed", "access_blocked", "observed_at",
        "source_report_id", "quoted_span", "confidence", "evidence_digest",
    )
    required_any_fields: ClassVar[tuple[tuple[str, ...], ...]] = ()
