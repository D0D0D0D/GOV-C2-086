"""S-3 transformation-before-render and independent validation coverage."""

from __future__ import annotations

import copy
import csv
import io

import pytest
import yaml

from framework.errors import SecurityViolationError

from src.nodes.post_process_node import PostProcessNode
from src.services.domain_utils import evidence_digest
from tests.domain_fixtures import CLOCK, SCOPE, SESSION


def _node_and_store():
    config = yaml.safe_load(open("config/config.yaml"))
    return PostProcessNode(config=config), None


def _state(*, finding="", actions=None, claims=None, facility_name="中央第一小学校", observation=None, conflicts=None):
    observation = observation or {
        "observation_id": "obs-alpha", "category": "other", "severity_observed": "unknown",
        "access_blocked": False, "observed_at": CLOCK, "source_report_id": "REP-A",
        "quoted_span": "中央第一小学校で被災状況を確認", "confidence": 1.0,
        "evidence_digest": evidence_digest("中央第一小学校で被災状況を確認"),
    }
    return {
        "status": "success", "request_mode": "invoke", "mode": "invoke", "scope": SCOPE,
        "request_clock": CLOCK, "session_id": SESSION, "facility_id_snapshot": ["FAC-A"],
        "facility_status_snapshot": [{
            "facility_id": "FAC-A", "facility_name": facility_name, "importance": "critical",
            "damage_observations": [observation], "conflicts": conflicts or [],
        }],
        "urgency_evaluations": [{
            "facility_id": "FAC-A", "urgency": "normal", "applied_rule_id": "U_DEFAULT",
            "conflict_pending": False,
        }],
        "briefs": [{
            "facility_id": "FAC-A", "finding": finding, "required_actions": actions or [], "claims": claims or [],
        }],
        "unresolved": [], "degradation_reason": [], "rejected": [],
    }


def test_assertive_required_action_is_downgraded_once_before_csv_render():
    node, _ = _node_and_store()
    result = node.execute(_state(actions=["点検は不要です。点検不要です。"]))
    facility = result["formatted_output"]["facilities"][0]
    assert facility["needs_confirmation"] is True
    assert facility["required_actions"][0].count("（要確認）") == 2
    assert "（要確認）" in result["formatted_output"]["csv_document"]


def test_llm_generated_pii_sentence_is_removed_before_csv_render():
    node, _ = _node_and_store()
    result = node.execute(_state(finding="氏名: 山田太郎が安全です。"))
    formatted = result["formatted_output"]
    assert "山田太郎" not in formatted["facilities"][0]["damage_summary"]
    assert "山田太郎" not in formatted["csv_document"]
    assert any(item["reason_code"] == "E_PII_OUTPUT" for item in formatted["unresolved"])


def test_numeric_claim_passes_and_uncited_hallucinated_number_is_removed():
    node, _ = _node_and_store()
    text = "中央第一小学校で3棟に被害を確認"
    observation = {
        "observation_id": "obs-alpha", "category": "building", "severity_observed": "partial_damage",
        "access_blocked": False, "observed_at": CLOCK, "source_report_id": "REP-A", "quoted_span": text,
        "confidence": 0.9, "evidence_digest": evidence_digest(text),
    }
    claim = {
        "field_path": "finding#sentence[0]", "observation_id": "obs-alpha",
    }
    kept = node.execute(_state(finding="3棟に被害。", claims=[claim], observation=observation))
    kept_facility = kept["formatted_output"]["facilities"][0]
    assert kept_facility["damage_summary"] == "3棟に被害。"
    assert kept_facility["claims"] == [{
        "field_path": "damage_summary#sentence[0]",
        "observation_id": "obs-alpha",
        "source_report_id": "REP-A",
        "quoted_span": text,
        "kind": "observation",
    }]
    removed = node.execute(_state(finding="4棟に被害。", claims=[claim], observation=observation))
    removed_formatted = removed["formatted_output"]
    assert removed["status"] == "success"
    assert removed_formatted["facilities"][0]["damage_summary"] == ""
    assert "4棟" not in removed_formatted["csv_document"]
    assert any(item["reason_code"] == "E_CLAIM_UNVERIFIED" for item in removed_formatted["unresolved"])
    node._extra_security_gate_output(removed)


def test_unknown_observation_id_discards_only_that_claim():
    node, _ = _node_and_store()
    valid = {"field_path": "finding", "observation_id": "obs-alpha"}
    unknown = {"field_path": "finding", "observation_id": "obs-missing"}

    result = node.execute(_state(finding="通常所見。", claims=[valid, unknown]))
    formatted = result["formatted_output"]

    assert formatted["facilities"][0]["damage_summary"] == "通常所見。"
    assert [claim["observation_id"] for claim in formatted["facilities"][0]["claims"]] == ["obs-alpha"]
    assert sum(item["reason_code"] == "E_CLAIM_UNVERIFIED" for item in formatted["unresolved"]) == 1


@pytest.mark.parametrize(
    ("observation_overrides", "conflicts", "expected_kind"),
    [
        ({"access_blocked": True}, [], "access"),
        ({"category": "building"}, [{"category": "building"}], "conflict"),
        ({"category": "building"}, [], "observation"),
    ],
)
def test_public_claim_kind_is_derived_from_persistent_observation(
    observation_overrides, conflicts, expected_kind
):
    node, _ = _node_and_store()
    base = _state()["facility_status_snapshot"][0]["damage_observations"][0]
    observation = {**base, **observation_overrides}
    claim = {"field_path": "finding", "observation_id": observation["observation_id"]}

    result = node.execute(
        _state(finding="通常所見。", claims=[claim], observation=observation, conflicts=conflicts)
    )

    assert result["formatted_output"]["facilities"][0]["claims"][0]["kind"] == expected_kind


def test_brief_prose_empty_degradation_has_empty_and_nonempty_controls():
    node, _ = _node_and_store()
    empty_state = _state()
    second_observation = {
        **copy.deepcopy(empty_state["facility_status_snapshot"][0]["damage_observations"][0]),
        "observation_id": "obs-beta",
        "source_report_id": "REP-B",
    }
    empty_state["facility_id_snapshot"].append("FAC-B")
    empty_state["facility_status_snapshot"].append({
        "facility_id": "FAC-B",
        "facility_name": "東部第二中学校",
        "importance": "normal",
        "damage_observations": [second_observation],
        "conflicts": [],
    })
    empty_state["urgency_evaluations"].append({
        "facility_id": "FAC-B",
        "urgency": "normal",
        "applied_rule_id": "U_DEFAULT",
        "conflict_pending": False,
    })
    empty_state["briefs"].append({
        "facility_id": "FAC-B", "finding": "", "required_actions": [], "claims": [],
    })
    nonempty_state = copy.deepcopy(empty_state)
    nonempty_state["briefs"][1]["finding"] = "通常所見。"

    empty = node.execute(empty_state)["formatted_output"]
    nonempty = node.execute(nonempty_state)["formatted_output"]

    assert "brief_prose_empty" in empty["degradation_reason"]
    assert "brief_prose_empty" not in nonempty["degradation_reason"]


def test_csv_is_rfc4180_deterministic_and_neutralizes_formula_cells():
    node, _ = _node_and_store()
    first = node.execute(_state(finding="通常所見", facility_name="=CMD"))["formatted_output"]["csv_document"]
    second = node.execute(_state(finding="通常所見", facility_name="=CMD"))["formatted_output"]["csv_document"]
    assert first == second
    rows = list(csv.DictReader(io.StringIO(first)))
    assert rows[0]["facility_name"] == "'=CMD"
    assert rows[0]["advisory_notice"]


def test_s3_hook_is_validation_only_except_guard_context_removal():
    node, _ = _node_and_store()
    result = node.execute(_state(finding="通常所見"))
    expected = copy.deepcopy(result["formatted_output"])
    gated = node._extra_security_gate_output(result)
    assert gated["formatted_output"] == expected
    assert "_guard_context" not in gated


def test_s3_hook_fails_closed_for_missing_guard_or_unknown_output_field():
    node, _ = _node_and_store()
    missing = node.execute(_state())
    missing.pop("_guard_context")
    with pytest.raises(SecurityViolationError, match="guard_context"):
        node._extra_security_gate_output(missing)
    unknown = node.execute(_state())
    unknown["formatted_output"]["new_free_text"] = "not declared"
    with pytest.raises(SecurityViolationError, match="shape"):
        node._extra_security_gate_output(unknown)


def test_s3_hook_independently_rejects_numeric_tampering_after_execute():
    node, _ = _node_and_store()
    text = "中央第一小学校で3棟に被害を確認"
    observation = {
        "observation_id": "obs-alpha", "category": "building", "severity_observed": "partial_damage",
        "access_blocked": False, "observed_at": CLOCK, "source_report_id": "REP-A", "quoted_span": text,
        "confidence": 0.9, "evidence_digest": evidence_digest(text),
    }
    claim = {
        "field_path": "finding", "observation_id": "obs-alpha",
    }
    result = node.execute(_state(finding="3棟に被害。", claims=[claim], observation=observation))
    control = copy.deepcopy(result)
    gated = node._extra_security_gate_output(control)
    assert gated["formatted_output"]["facilities"][0]["claims"][0]["field_path"] == "damage_summary"
    result["formatted_output"]["facilities"][0]["damage_summary"] = "4棟に被害。"
    with pytest.raises(SecurityViolationError, match="uncited numeric"):
        node._extra_security_gate_output(result)
