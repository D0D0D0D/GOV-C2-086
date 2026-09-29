"""Deterministic urgency, as-of, and conflict predicate tests."""

import yaml

from src.nodes.inner.reconcile_persist import ReconcilePersistNode
from src.nodes.inner.status_load import StatusLoadNode
from src.nodes.inner.urgency_evaluate import UrgencyEvaluateNode
from src.services.domain_utils import stable_id
from src.services.repository import SQLiteFacilityStatusRepository
from tests.domain_fixtures import CLOCK, REGISTRY, SCOPE


def _resolved(*, report, blocked, severity="unknown"):
    return {
        "extraction_item_id": stable_id(report, 0), "facility_mention": "中央第一小学校",
        "facility_id": "FAC-A", "category": "access", "severity_observed": severity,
        "access_blocked": blocked, "observed_at": CLOCK, "source_report_id": report,
        "quoted_span": "中央第一小学校への進入路を確認", "confidence": 1.0,
        "evidence_digest": "abcdef0123456789",
    }


def test_access_blocked_only_change_is_conflict_and_both_observations_survive():
    repository = SQLiteFacilityStatusRepository(":memory:", registry_seed=REGISTRY)
    node = ReconcilePersistNode(repository=repository)
    base_state = {
        "status": "success", "scope": SCOPE, "request_clock": CLOCK, "rejected": [],
        "review_queue_delta": [], "degradation_reason": [],
    }
    first = node.execute(base_state | {"resolved_observations": [_resolved(report="REP-A", blocked=False)]})
    second = node.execute(base_state | {"resolved_observations": [_resolved(report="REP-B", blocked=True)]})
    status = repository.load_status(SCOPE)[0]
    assert len(status["damage_observations"]) == 2
    assert status["conflicts"][0]["state"] == "open"
    assert second["review_queue_delta"][0]["reason_code"] == "R_CONFLICT"
    assert first["ingest_summary"]["written_ids"] != second["ingest_summary"]["written_ids"]


def test_urgency_uses_latest_group_heaviest_severity_and_access_or():
    config = yaml.safe_load(open("config/config.yaml"))
    snapshot = [{
        "facility_id": "FAC-A", "facility_name": "中央第一小学校", "importance": "critical",
        "damage_observations": [
            {"observation_id": "a", "category": "building", "observed_at": CLOCK, "severity_observed": "partial_damage", "access_blocked": False},
            {"observation_id": "b", "category": "building", "observed_at": CLOCK, "severity_observed": "structural_damage", "access_blocked": True},
        ],
        "conflicts": [{"state": "open"}],
    }]
    result = UrgencyEvaluateNode(config=config).execute({"status": "success", "facility_status_snapshot": snapshot})
    evaluation = result["urgency_evaluations"][0]
    assert evaluation == {
        "facility_id": "FAC-A", "urgency": "immediate", "applied_rule_id": "U1", "conflict_pending": True,
    }


def test_status_load_excludes_future_observation_once_at_as_of_boundary():
    repository = SQLiteFacilityStatusRepository(":memory:", registry_seed=REGISTRY)
    future = "2026-08-21T00:00:00Z"
    observation = {
        "facility_id": "FAC-A", "updated_at": future,
        "observation_id": stable_id("FAC-A", "other", future, "REP-F", "unknown", False, "中央第一小学校を確認"),
        "category": "other", "severity_observed": "unknown", "access_blocked": False,
        "observed_at": future, "source_report_id": "REP-F", "quoted_span": "中央第一小学校を確認",
        "confidence": 1.0, "evidence_digest": "abcdef0123456789",
    }
    repository.apply_ingest(SCOPE, observations=[observation], conflicts=[], review_items=[], audit_records=[])
    result = StatusLoadNode(repository=repository).execute(
        {"status": "success", "scope": SCOPE, "facility_id_snapshot": ["FAC-A"], "as_of": CLOCK}
    )
    assert result["facility_status_snapshot"][0]["damage_observations"] == []
