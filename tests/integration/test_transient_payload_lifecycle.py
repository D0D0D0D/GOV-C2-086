"""Cross-request provenance and transient payload lifecycle contracts."""

from __future__ import annotations

import json
import sqlite3

import pytest

from shared.services.llm.base_llm import BaseLLM

from src.payload_store.payload_store import PayloadStore, PayloadStoreError
from src.services.domain_utils import evidence_digest
from tests.domain_fixtures import (
    CALLER,
    CLOCK,
    SCOPE,
    build_runtime,
    ingest_one,
    internal_envelope,
    invocation_context,
    invoke_envelope,
)


QUOTED_SPAN = "中央第一小の体育館で床上30cmの浸水を確認しました。"
RAW_SENTINEL = "NON_RETAINED_RAW_REPORT_SENTINEL"
RAW_REPORT = f"{QUOTED_SPAN}{RAW_SENTINEL}"


class GroundedWorkflowLlm(BaseLLM):
    """Return contract-valid extraction and brief values derived from each prompt."""

    def complete(self, messages: list) -> dict:
        request = json.loads(messages[0]["content"])
        if "masked_report" in request:
            content = {
                "observations": [{
                    "item_index": 0,
                    "facility_mention": "中央第一小",
                    "category": "building",
                    "severity_observed": "partial_damage",
                    "access_blocked": False,
                    "observed_at": None,
                    "quoted_span": QUOTED_SPAN,
                    "confidence": 0.9,
                }]
            }
        elif "facility_status_snapshot" in request:
            snapshot = request["facility_status_snapshot"][0]
            observation = snapshot["damage_observations"][0]
            content = {
                "briefs": [{
                    "facility_id": snapshot["facility_id"],
                    "finding": "床上30cmの浸水が確認された。",
                    "required_actions": [],
                    "claims": [{
                        "field_path": "finding",
                        "observation_id": observation["observation_id"],
                    }],
                }]
            }
        else:
            raise AssertionError("unexpected entity scoring call")
        return {
            "content": json.dumps(content, ensure_ascii=False),
            "tool_calls": [],
            "model": "fake",
            "usage": {},
        }

    def stream(self, messages: list):
        yield ""

    def bind_tools(self, tools: list):
        return self


def _invoke_facility(graph, store, *, session_id: str):
    return invoke_envelope(
        graph,
        store,
        invocation_context(session_id),
        internal_envelope("invoke", facility_id_snapshot=["FAC-A"]),
    )


def test_provenance_is_self_contained_across_distinct_request_sessions():
    graph, repository, store, ingest_context = build_runtime(llm=GroundedWorkflowLlm())
    ingest, _ = ingest_one(graph, store, ingest_context, text=RAW_REPORT, report_id="REP-CROSS")
    assert ingest["status"] == "success"

    invoke = _invoke_facility(graph, store, session_id="session-invoke-distinct")

    assert ingest_context.session_id != "session-invoke-distinct"
    assert invoke["status"] == "success"
    facility = invoke["output"]["facilities"][0]
    assert facility["damage_summary"] == "床上30cmの浸水が確認された。"
    assert facility["claims"][0]["source_report_id"] == "REP-CROSS"
    assert not any(item["reason_code"] == "E_CLAIM_UNVERIFIED" for item in invoke["output"]["unresolved"])


def test_repository_reopen_preserves_status_queue_audit_and_invoke_result(tmp_path):
    path = tmp_path / "restart.sqlite3"
    graph, repository, store, ingest_context = build_runtime(path, llm=GroundedWorkflowLlm())
    ingest, _ = ingest_one(graph, store, ingest_context, text=RAW_REPORT, report_id="REP-RESTART")
    assert ingest["status"] == "success"

    queue = {
        "queue_id": "queue-restart",
        "disaster_event_id": SCOPE["disaster_event_id"],
        "source_report_id": "REP-REVIEW",
        "reason_code": "R_LOW_CONFIDENCE",
        "candidates": [],
        "reason_note": "",
        "state": "open",
        "created_at": CLOCK,
        "resolved_by": None,
        "resolved_at": None,
    }
    audit = {
        "audit_id": "audit-restart",
        "disaster_event_id": SCOPE["disaster_event_id"],
        "action": "resolve_queue",
        "target_id": "queue-restart",
        "before": {},
        "after": {"state": "open"},
        "actor": CALLER,
        "at": CLOCK,
        "note_raw": None,
        "note_audit": None,
    }
    repository.apply_ingest(
        SCOPE,
        observations=[],
        conflicts=[],
        review_items=[queue],
        audit_records=[audit],
    )
    before = _invoke_facility(graph, store, session_id="session-before-restart")
    repository.close()

    reopened_graph, reopened_repository, reopened_store, _ = build_runtime(
        path, llm=GroundedWorkflowLlm(), registry=()
    )
    after = _invoke_facility(reopened_graph, reopened_store, session_id="session-after-restart")

    assert after["output"] == before["output"]
    assert reopened_repository.load_status(SCOPE)[0]["damage_observations"]
    assert reopened_repository.load_review_queue(SCOPE) == [queue]
    with sqlite3.connect(path) as connection:
        stored_audit = json.loads(
            connection.execute(
                "SELECT document FROM audit_record WHERE disaster_event_id = ? AND audit_id = ?",
                (SCOPE["disaster_event_id"], "audit-restart"),
            ).fetchone()[0]
        )
    assert stored_audit == audit
    reopened_repository.close()


def test_payload_ref_is_request_local_and_expires_at_configured_ttl():
    now = [0.0]
    store = PayloadStore(
        scope_keys=("disaster_event_id",),
        ttl_seconds=60,
        clock=lambda: now[0],
    )
    ref = store.put("transient report", scope=SCOPE, session_id="request-one")

    assert store.resolve(ref, scope=SCOPE, session_id="request-one") == "transient report"
    with pytest.raises(PayloadStoreError, match="^payload_ref_scope_mismatch$"):
        store.resolve(ref, scope=SCOPE, session_id="request-two")
    now[0] = 61.0
    with pytest.raises(PayloadStoreError, match="^payload_ref_expired$"):
        store.resolve(ref, scope=SCOPE, session_id="request-one")


def test_payload_ref_cross_scope_resolution_is_rejected():
    store = PayloadStore(scope_keys=("disaster_event_id",), ttl_seconds=300)
    ref = store.put("transient report", scope=SCOPE, session_id="request-one")

    with pytest.raises(PayloadStoreError, match="^payload_ref_scope_mismatch$"):
        store.resolve(
            ref,
            scope={"disaster_event_id": "event-other"},
            session_id="request-one",
        )


def test_ingest_persists_neither_raw_report_nor_payload_ref_and_mutant_is_rejected():
    graph, repository, store, context = build_runtime(llm=GroundedWorkflowLlm())
    ingest, payload_ref = ingest_one(
        graph,
        store,
        context,
        text=RAW_REPORT,
        report_id="REP-NONRETAINED",
    )
    assert ingest["status"] == "success"

    statuses = repository.load_status(SCOPE)
    persisted_documents = [
        row[0]
        for table in ("facility_status", "review_queue", "audit_record")
        for row in repository._connection.execute(f"SELECT document FROM {table}").fetchall()
    ]
    serialized = json.dumps(persisted_documents, ensure_ascii=False, sort_keys=True)
    observation = statuses[0]["damage_observations"][0]
    assert RAW_REPORT not in serialized
    assert RAW_SENTINEL not in serialized
    assert "payload_ref" not in serialized
    assert observation["quoted_span"] == QUOTED_SPAN
    assert observation["evidence_digest"] == evidence_digest(QUOTED_SPAN)
    with pytest.raises(PayloadStoreError, match="^payload_ref_replayed$"):
        store.resolve(payload_ref, scope=SCOPE, session_id=context.session_id)

    mutant = {
        "facility_id": "FAC-A",
        "updated_at": CLOCK,
        **observation,
        "payload_ref": payload_ref,
    }
    with pytest.raises(ValueError, match="E_SCHEMA_UNKNOWN_FIELD"):
        repository.apply_ingest(
            SCOPE,
            observations=[mutant],
            conflicts=[],
            review_items=[],
            audit_records=[],
        )
