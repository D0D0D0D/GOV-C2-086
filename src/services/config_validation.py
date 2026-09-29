"""Executable validation for the domain configuration surface."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from framework.errors import ConfigError
from shared.services.llm.base_llm import BaseLLM


DEFAULTS: dict[str, Any] = {
    "resolution_confidence_threshold": 0.85,
    "resolution_margin": 0.15,
    "candidate_top_k": 10,
    "candidate_min_bigram_hits": 2,
    "min_quoted_span_chars": 8,
    "max_quoted_span_chars": 200,
    "max_report_chars": 8000,
    "max_records_per_request": 100,
    "max_note_chars": 500,
    "max_replacement_char_ratio": 0.02,
    "max_control_char_ratio": 0.01,
    "min_printable_ratio": 0.90,
    "payload_ttl_seconds": 300,
    "business_timezone": "Asia/Tokyo",
    "repository_path": "./data/gov_c2_086.sqlite3",
    "assertive_phrase_action": "downgrade",
    "llm": None,
}

_FRAMEWORK_KEYS = {"max_retry", "memory_enabled", "hitl"}
_RUNTIME_INJECTION_KEYS = {"repository", "payload_store"}
_ALLOWED = set(DEFAULTS) | _FRAMEWORK_KEYS | _RUNTIME_INJECTION_KEYS | {"urgency_rules"}
_URGENCIES = {"immediate", "high", "normal"}
_IMPORTANCE = {"critical", "high", "normal"}
_SEVERITIES = {"structural_damage", "partial_damage", "utility_outage", "no_visible_damage", "unknown"}
_CATEGORIES = {"building", "utility", "access", "equipment", "other"}
_WHEN_ENUMS = {
    "importance": _IMPORTANCE,
    "severity_observed": _SEVERITIES,
    "category": _CATEGORIES,
}


def validate_domain_config(config: dict[str, Any] | None) -> dict[str, Any]:
    """Validate all declared keys and return one normalised snapshot."""

    if config is None:
        config = {}
    if not isinstance(config, dict):
        raise ConfigError("E_CONFIG_TYPE: config must be a dict")
    unknown = set(config) - _ALLOWED
    if unknown:
        raise ConfigError("E_CONFIG_UNKNOWN_KEY: undeclared configuration key")

    result = copy.deepcopy(DEFAULTS)
    for key, value in config.items():
        if key in {"llm", "repository", "payload_store"}:
            result[key] = value
        else:
            result[key] = copy.deepcopy(value)

    if "urgency_rules" not in result:
        raise ConfigError("E_CONFIG_MISSING: urgency_rules is required")
    _validate_number(result, "resolution_confidence_threshold", 0.0, 1.0)
    _validate_number(result, "resolution_margin", 0.0, 1.0)
    _validate_int(result, "candidate_top_k", 1, 100)
    _validate_int(result, "candidate_min_bigram_hits", 1, 20)
    _validate_int(result, "min_quoted_span_chars", 1, 199)
    _validate_int(result, "max_quoted_span_chars", 2, 100_000)
    if result["min_quoted_span_chars"] >= result["max_quoted_span_chars"]:
        raise ConfigError("E_CONFIG_RANGE: min_quoted_span_chars must be less than max_quoted_span_chars")
    _validate_int(result, "max_report_chars", 1, 100_000)
    _validate_int(result, "max_records_per_request", 1, 500)
    _validate_int(result, "max_note_chars", 1, 5_000)
    _validate_number(result, "max_replacement_char_ratio", 0.0, 1.0)
    _validate_number(result, "max_control_char_ratio", 0.0, 1.0)
    _validate_number(result, "min_printable_ratio", 0.0, 1.0)
    _validate_int(result, "payload_ttl_seconds", 60, 3_600)

    if not isinstance(result["business_timezone"], str):
        raise ConfigError("E_CONFIG_TYPE: business_timezone must be str")
    try:
        ZoneInfo(result["business_timezone"])
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError("E_CONFIG_ENUM: business_timezone must be an IANA zone") from None
    if not isinstance(result["repository_path"], str) or not result["repository_path"].strip():
        raise ConfigError("E_CONFIG_TYPE: repository_path must be a non-empty str")
    parent = Path(result["repository_path"]).expanduser().parent
    if parent.exists() and not parent.is_dir():
        raise ConfigError("E_CONFIG_RANGE: repository_path parent is not a directory")
    if result["assertive_phrase_action"] != "downgrade":
        raise ConfigError("E_CONFIG_ENUM: assertive_phrase_action must be downgrade")
    llm = result.get("llm")
    if llm is not None and not isinstance(llm, BaseLLM):
        raise ConfigError("E_CONFIG_TYPE: llm must be BaseLLM or None")
    _validate_urgency_rules(result["urgency_rules"])
    return result


def _validate_int(config: dict[str, Any], key: str, low: int, high: int) -> None:
    value = config[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"E_CONFIG_TYPE: {key} must be int")
    if not low <= value <= high:
        raise ConfigError(f"E_CONFIG_RANGE: {key} outside [{low}, {high}]")


def _validate_number(config: dict[str, Any], key: str, low: float, high: float) -> None:
    value = config[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"E_CONFIG_TYPE: {key} must be a number")
    if not low <= float(value) <= high:
        raise ConfigError(f"E_CONFIG_RANGE: {key} outside [{low}, {high}]")
    config[key] = float(value)


def _validate_urgency_rules(value: Any) -> None:
    if not isinstance(value, dict):
        raise ConfigError("E_CONFIG_TYPE: urgency_rules must be a dict")
    if "default_urgency" not in value or "rules" not in value:
        raise ConfigError("E_CONFIG_MISSING: urgency_rules.default_urgency and rules are required")
    if set(value) != {"default_urgency", "rules"}:
        raise ConfigError("E_CONFIG_UNKNOWN_KEY: urgency_rules contains undeclared keys")
    if value["default_urgency"] not in _URGENCIES:
        raise ConfigError("E_CONFIG_ENUM: invalid default_urgency")
    if not isinstance(value["rules"], list):
        raise ConfigError("E_CONFIG_TYPE: urgency_rules.rules must be a list")
    seen: set[str] = set()
    for rule in value["rules"]:
        if not isinstance(rule, dict) or set(rule) != {"id", "when", "urgency"}:
            raise ConfigError("E_CONFIG_TYPE: each urgency rule must contain id, when, urgency")
        rule_id = rule["id"]
        if not isinstance(rule_id, str) or not rule_id or rule_id in seen or rule_id == "U_DEFAULT":
            raise ConfigError("E_CONFIG_RULE_ID: urgency rule ids must be unique")
        seen.add(rule_id)
        if rule["urgency"] not in _URGENCIES:
            raise ConfigError("E_CONFIG_ENUM: invalid rule urgency")
        when = rule["when"]
        if not isinstance(when, dict) or not when:
            raise ConfigError("E_CONFIG_TYPE: rule when must be a non-empty dict")
        if set(when) - {"importance", "severity_observed", "category", "access_blocked"}:
            raise ConfigError("E_CONFIG_RULE_KEY: rule when contains an undeclared key")
        for key, item in when.items():
            if key == "access_blocked":
                if not isinstance(item, bool):
                    raise ConfigError("E_CONFIG_TYPE: access_blocked rule value must be bool")
            elif not isinstance(item, list) or not item or any(v not in _WHEN_ENUMS[key] for v in item):
                raise ConfigError("E_CONFIG_ENUM: invalid urgency rule enum")
