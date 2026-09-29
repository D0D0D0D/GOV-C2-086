"""SQLite implementation contract at the FacilityStatusRepository swap point."""

from __future__ import annotations

import pytest

from src.services.domain_utils import stable_id
from src.services.repository import SQLiteFacilityStatusRepository
from tests.domain_fixtures import CLOCK, REGISTRY, SCOPE


def _observation(*, blocked=False, severity="unknown", report="REP-A", span="中央第一小学校への進入路を確認"):
    observation_id = stable_id("FAC-A", "access", CLOCK, report, severity, blocked, span)
    return {
        "facility_id": "FAC-A", "updated_at": CLOCK, "observation_id": observation_id,
        "category": "access", "severity_observed": severity, "access_blocked": blocked,
        "observed_at": CLOCK, "source_report_id": report, "quoted_span": span, "confidence": 1.0,
        "evidence_digest": "abcdef0123456789",
    }


def test_repository_is_empty_and_scope_filtered_fail_closed():
    repository = SQLiteFacilityStatusRepository(":memory:", registry_seed=REGISTRY)
    assert repository.load_status(SCOPE) == []
    repository.apply_ingest(SCOPE, observations=[_observation()], conflicts=[], review_items=[], audit_records=[])
    assert len(repository.load_status(SCOPE)) == 1
    assert repository.load_status({"disaster_event_id": "event-other"}) == []
    assert repository.load_status(SCOPE, []) == []


def test_repository_deduplicates_identical_observation_and_keeps_conflicting_both():
    repository = SQLiteFacilityStatusRepository(":memory:", registry_seed=REGISTRY)
    first = _observation(blocked=False)
    assert repository.apply_ingest(SCOPE, observations=[first], conflicts=[], review_items=[], audit_records=[]) == [first["observation_id"]]
    assert repository.apply_ingest(SCOPE, observations=[first], conflicts=[], review_items=[], audit_records=[]) == []
    second = _observation(blocked=True, report="REP-B")
    conflict_id = stable_id(SCOPE["disaster_event_id"], "FAC-A", "access", CLOCK)
    conflict = {
        "facility_id": "FAC-A", "conflict_id": conflict_id, "category": "access",
        "observation_ids": sorted([first["observation_id"], second["observation_id"]]),
        "note": "同一時点の観測値が矛盾しています。", "detected_at": CLOCK, "state": "open",
    }
    repository.apply_ingest(SCOPE, observations=[second], conflicts=[conflict], review_items=[], audit_records=[])
    status = repository.load_status(SCOPE)[0]
    assert len(status["damage_observations"]) == 2
    assert status["conflicts"][0]["observation_ids"] == conflict["observation_ids"]


def test_repository_file_backend_survives_reopen(tmp_path):
    path = tmp_path / "status.sqlite3"
    first = SQLiteFacilityStatusRepository(str(path), registry_seed=REGISTRY)
    first.apply_ingest(SCOPE, observations=[_observation()], conflicts=[], review_items=[], audit_records=[])
    reopened = SQLiteFacilityStatusRepository(str(path))
    assert reopened.load_status(SCOPE)[0]["damage_observations"][0]["source_report_id"] == "REP-A"


def test_repository_unknown_persistent_field_aborts_whole_batch():
    repository = SQLiteFacilityStatusRepository(":memory:", registry_seed=REGISTRY)
    bad = _observation() | {"undeclared": "must not be silently discarded"}
    with pytest.raises(ValueError, match="E_SCHEMA_UNKNOWN_FIELD"):
        repository.apply_ingest(SCOPE, observations=[bad], conflicts=[], review_items=[], audit_records=[])
    assert repository.load_status(SCOPE) == []


def test_repository_port_exposes_only_atomic_write_methods():
    repository = SQLiteFacilityStatusRepository(":memory:")
    assert hasattr(repository, "apply_ingest") and hasattr(repository, "apply_feedback")
    assert not hasattr(repository, "upsert_status")
    assert not hasattr(repository, "enqueue_review")
    assert not hasattr(repository, "append_audit")
    assert not hasattr(repository, "write_registry")
    assert not hasattr(repository, "resolve_report_evidence")


def test_apply_ingest_rolls_back_status_when_audit_step_fails():
    class FailingAuditRepository(SQLiteFacilityStatusRepository):
        def _insert_audit(self, conn, event_id, records):
            raise RuntimeError("injected audit failure")

    repository = FailingAuditRepository(":memory:", registry_seed=REGISTRY)
    audit = {
        "audit_id": "audit-alpha", "disaster_event_id": SCOPE["disaster_event_id"],
        "action": "resolve_queue", "target_id": "queue-alpha", "before": {}, "after": {},
        "actor": "caller-alpha", "at": CLOCK, "note_raw": None, "note_audit": None,
    }
    with pytest.raises(RuntimeError, match="injected"):
        repository.apply_ingest(
            SCOPE, observations=[_observation()], conflicts=[], review_items=[], audit_records=[audit]
        )
    assert repository.load_status(SCOPE) == []
