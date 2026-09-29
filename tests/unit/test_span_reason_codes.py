"""Span lower/upper bounds must remain operationally distinguishable."""

from __future__ import annotations

import json

import pytest

from src.nodes.inner.entity_resolve import EntityResolveNode
from src.nodes.inner.intake_extract import IntakeExtractNode
from src.nodes.post_process_node import PostProcessNode
from src.payload_store.payload_store import PayloadStore
from src.services.payload_broker import ScopedPayloadBroker
from tests.domain_fixtures import CLOCK, REGISTRY, SCOPE, SESSION


class SpanLlm:
    def __init__(self, span: str) -> None:
        self.span = span

    def complete(self, _messages):
        return {
            "content": json.dumps(
                {
                    "observations": [
                        {
                            "item_index": 0,
                            "facility_mention": "対象施設",
                            "category": "building",
                            "severity_observed": "partial_damage",
                            "access_blocked": False,
                            "observed_at": None,
                            "quoted_span": self.span,
                            "confidence": 0.9,
                        }
                    ]
                },
                ensure_ascii=False,
            )
        }


class EmptyRegistry:
    def load_registry(self):
        return []


@pytest.mark.parametrize(
    ("span", "reason_code"),
    [("短文", "E_SPAN_TOO_SHORT"), ("長" * 201, "E_SPAN_TOO_LONG")],
)
def test_intake_extract_places_span_reason_in_rejected(span, reason_code):
    store = ScopedPayloadBroker(PayloadStore(scope_keys=("disaster_event_id",), ttl_seconds=300))
    payload_ref = store.put(
        {"report_id": "REP-SPAN", "text": span, "reported_at": CLOCK, "channel": "field_memo"},
        scope=SCOPE,
        session_id=SESSION,
    )
    node = IntakeExtractNode(
        config={
            "llm": SpanLlm(span),
            "min_quoted_span_chars": 8,
            "max_quoted_span_chars": 200,
            "max_report_chars": 1000,
            "min_printable_ratio": 0.90,
            "max_replacement_char_ratio": 0.02,
            "max_control_char_ratio": 0.01,
        },
        payload_store=store,
        repository=EmptyRegistry(),
    )
    result = node.execute(
        {
            "status": "success",
            "scope": SCOPE,
            "session_id": SESSION,
            "request_clock": CLOCK,
            "report_refs": [{"row_index": 0, "report_id": "REP-SPAN", "payload_ref": payload_ref}],
            "rejected": [],
            "review_queue_delta": [],
            "degradation_reason": [],
        }
    )
    assert result["rejected"][0]["reason_code"] == reason_code


def test_entity_resolve_places_oversized_candidate_span_in_rejected():
    class Repository:
        def load_registry(self):
            return REGISTRY

    class CandidateLlm:
        def complete(self, _messages):
            return {
                "content": json.dumps(
                    {
                        "resolutions": [
                            {
                                "extraction_item_id": "item-span",
                                "candidates": [
                                    {"facility_id": "FAC-A", "score": 0.99, "quoted_span": "長" * 201}
                                ],
                            }
                        ]
                    },
                    ensure_ascii=False,
                )
            }

    item = {
        "extraction_item_id": "item-span",
        "facility_mention": "未確定施設",
        "category": "building",
        "severity_observed": "partial_damage",
        "access_blocked": False,
        "observed_at": CLOCK,
        "source_report_id": "REP-SPAN",
        "quoted_span": "中央第一小学校の体育館で浸水を確認",
        "confidence": 0.9,
        "evidence_digest": "abcdef0123456789",
    }
    node = EntityResolveNode(
        config={
            "llm": CandidateLlm(),
            "candidate_min_bigram_hits": 2,
            "candidate_top_k": 10,
            "min_quoted_span_chars": 8,
            "max_quoted_span_chars": 200,
            "resolution_confidence_threshold": 0.85,
            "resolution_margin": 0.15,
        },
        repository=Repository(),
    )
    result = node.execute(
        {
            "status": "success",
            "scope": SCOPE,
            "request_clock": CLOCK,
            "report_refs": [{"row_index": 4, "report_id": "REP-SPAN", "payload_ref": "a" * 32}],
            "extracted_observations": [item],
            "review_queue_delta": [],
            "rejected": [],
            "degradation_reason": [],
        }
    )
    assert any(item["reason_code"] == "E_SPAN_TOO_LONG" for item in result["rejected"])


@pytest.mark.parametrize(
    ("span", "reason_code"),
    [("短文", "E_SPAN_TOO_SHORT"), ("長" * 201, "E_SPAN_TOO_LONG")],
)
def test_post_process_places_claim_span_reason_in_unresolved(span, reason_code):
    node = PostProcessNode(config={"min_quoted_span_chars": 8, "max_quoted_span_chars": 200})
    observation = {
        "observation_id": "obs-span",
        "category": "building",
        "severity_observed": "partial_damage",
        "access_blocked": False,
        "observed_at": CLOCK,
        "source_report_id": "REP-SPAN",
        "quoted_span": span,
        "confidence": 0.9,
        "evidence_digest": "abcdef0123456789",
    }
    claim = {
        "field_path": "finding#sentence[0]",
        "observation_id": "obs-span",
    }
    state = {
        "status": "success",
        "request_mode": "invoke",
        "mode": "invoke",
        "scope": SCOPE,
        "request_clock": CLOCK,
        "session_id": SESSION,
        "facility_id_snapshot": ["FAC-A"],
        "facility_status_snapshot": [
            {
                "facility_id": "FAC-A",
                "facility_name": "中央第一小学校",
                "importance": "critical",
                "damage_observations": [observation],
                "conflicts": [],
            }
        ],
        "urgency_evaluations": [
            {
                "facility_id": "FAC-A",
                "urgency": "normal",
                "applied_rule_id": "U_DEFAULT",
                "conflict_pending": False,
            }
        ],
        "briefs": [
            {"facility_id": "FAC-A", "finding": "通常所見", "required_actions": [], "claims": [claim]}
        ],
        "unresolved": [],
        "degradation_reason": [],
        "rejected": [],
    }
    result = node.execute(state)
    assert any(item["reason_code"] == reason_code for item in result["formatted_output"]["unresolved"])
