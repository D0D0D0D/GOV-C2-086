"""Prompt contracts expose every value needed to satisfy strict LLM output validation."""

from __future__ import annotations

import json

from shared.services.llm.base_llm import BaseLLM

from src.nodes.inner.brief_draft import (
    BriefDraftNode,
    _BRIEF_KEYS,
    _CLAIM_KEYS,
    _FIELD_PATH_ROOTS,
    _build_brief_prompt,
)
from src.nodes.inner.entity_resolve import _build_entity_resolution_prompt
from src.nodes.inner.intake_extract import (
    _CATEGORIES,
    _SEVERITIES,
    _build_intake_prompt,
)
from src.nodes.post_process_node import _field_path_matches, _to_public_field_path


class _StaticBriefLlm(BaseLLM):
    def __init__(self, claim):
        self._claim = claim

    def complete(self, messages: list) -> dict:
        return {
            "content": json.dumps({
                "briefs": [{
                    "facility_id": "FAC-A",
                    "finding": "通常所見",
                    "required_actions": [],
                    "claims": [self._claim],
                }]
            }, ensure_ascii=False),
            "tool_calls": [], "model": "fake", "usage": {},
        }

    def stream(self, messages: list):
        yield ""

    def bind_tools(self, tools: list):
        return self


def _brief_state():
    return {
        "status": "success",
        "facility_status_snapshot": [],
        "urgency_evaluations": [{"facility_id": "FAC-A"}],
        "unresolved": [],
        "degradation_reason": [],
    }


def test_intake_prompt_declares_all_enums_and_validation_constraints():
    prompt = _build_intake_prompt(
        "中央第一小の体育館で床上30cmの浸水を確認しました。",
        {"reported_at": "2026-08-20T00:00:00Z"},
        {"min_quoted_span_chars": 8, "max_quoted_span_chars": 200},
    )
    payload = json.loads(prompt)
    item_schema = payload["schema"]["observations"][0]

    assert item_schema["category"]["enum"] == sorted(_CATEGORIES)
    assert item_schema["severity_observed"]["enum"] == sorted(_SEVERITIES)
    assert set(item_schema) == {
        "item_index", "facility_mention", "category", "severity_observed", "access_blocked",
        "observed_at", "quoted_span", "confidence",
    }
    assert "zero-based contiguous" in payload["constraints"]["item_index"]
    assert "between 8 and 200" in payload["constraints"]["quoted_span"]
    assert "literal substring" in payload["constraints"]["quoted_span"]
    assert "Return null" in payload["constraints"]["observed_at"]
    assert "never infer" in payload["constraints"]["observed_at"]
    assert "0.0 through 1.0" in payload["constraints"]["confidence"]
    for value in _CATEGORIES | _SEVERITIES:
        assert value in prompt


def test_entity_resolution_prompt_binds_item_and_complete_candidate_allowlist():
    item = {
        "extraction_item_id": "item-001",
        "quoted_span": "中央第一小の体育館で床上30cmの浸水を確認しました。",
    }
    candidates = [
        ({"facility_id": "FAC-A", "name": "中央第一小学校", "aliases": ["中央第一小"]}, 4),
        ({"facility_id": "FAC-B", "name": "中央第二小学校", "aliases": []}, 2),
    ]
    prompt = _build_entity_resolution_prompt(item, candidates)
    payload = json.loads(prompt)

    assert payload["extraction_item_id"] == "item-001"
    assert payload["allowed_facility_ids"] == ["FAC-A", "FAC-B"]
    assert payload["schema"] == {
        "resolutions": [{
            "extraction_item_id": "string",
            "candidates": [{"facility_id": "string", "score": "number", "quoted_span": "string"}],
        }]
    }
    assert "exactly 'item-001'" in payload["constraints"]["extraction_item_id"]
    assert "allowed_facility_ids" in payload["constraints"]["facility_id"]
    assert "0.0 through 1.0" in payload["constraints"]["score"]
    assert "this same quoted_span" in payload["constraints"]["grounding"]
    for value in ("item-001", "FAC-A", "FAC-B"):
        assert value in prompt


def test_brief_prompt_declares_required_claim_shape_kinds_and_facility_allowlist():
    current = {
        "urgency_evaluations": [
            {"facility_id": "FAC-B", "urgency": "normal"},
            {"facility_id": "FAC-A", "urgency": "high"},
        ],
        "facility_status_snapshot": [{
            "facility_id": "FAC-A",
            "damage_observations": [{
                "observation_id": "OBS-1",
                "source_report_id": "REP-1",
                "quoted_span": "中央第一小で床上30cmの浸水を確認しました。",
            }],
        }],
    }
    prompt = _build_brief_prompt(current)
    payload = json.loads(prompt)
    brief_schema = payload["schema"]["briefs"][0]
    claim_schema = brief_schema["claims"][0]

    assert payload["allowed_facility_ids"] == ["FAC-A", "FAC-B"]
    assert set(brief_schema) == _BRIEF_KEYS
    assert set(claim_schema) == _CLAIM_KEYS
    assert claim_schema == {"field_path": "string", "observation_id": "string"}
    assert payload["constraints"]["required_keys"]["brief"] == sorted(_BRIEF_KEYS)
    assert payload["constraints"]["required_keys"]["claim"] == sorted(_CLAIM_KEYS)
    assert "same facility_status_snapshot" in payload["constraints"]["evidence"]
    schema_text = json.dumps(payload["schema"], ensure_ascii=False)
    for forbidden in ("quoted_span", "source_report_id", "kind", "asserted_values"):
        assert forbidden not in schema_text
    field_path_contract = payload["constraints"]["field_path"]
    for root in _FIELD_PATH_ROOTS:
        assert root in field_path_contract
    assert _field_path_matches(_to_public_field_path("finding"), "damage_summary", 0)
    assert _field_path_matches(
        _to_public_field_path("finding#sentence[0]"), "damage_summary", 0
    )
    assert _field_path_matches(
        _to_public_field_path("required_actions[0]"), "required_actions[0]", 0
    )


def test_brief_contract_accepts_only_field_path_and_observation_id():
    node = BriefDraftNode(config={"llm": _StaticBriefLlm({
        "field_path": "finding", "observation_id": "OBS-1",
    })})
    result = node.execute(_brief_state())

    assert result["briefs"][0]["claims"] == [{
        "field_path": "finding", "observation_id": "OBS-1",
    }]
    assert result["unresolved"] == []


def test_brief_contract_rejects_model_supplied_quoted_span_as_extra_key():
    node = BriefDraftNode(config={"llm": _StaticBriefLlm({
        "field_path": "finding",
        "observation_id": "OBS-1",
        "quoted_span": "モデルが書いた引用",
    })})
    result = node.execute(_brief_state())

    assert result["briefs"] == []
    assert result["unresolved"][0]["reason_code"] == "E_LLM_CONTRACT"
    assert "llm_contract_violation" in result["degradation_reason"]
