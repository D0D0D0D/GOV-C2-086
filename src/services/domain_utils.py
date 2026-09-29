"""Small deterministic helpers shared by domain nodes."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Any


def stable_id(*parts: Any) -> str:
    return hashlib.sha256("|".join(str(part) for part in parts).encode("utf-8")).hexdigest()[:16]


def evidence_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def normalize_name(value: str) -> str:
    return re.sub(r"[\s\W_]+", "", unicodedata.normalize("NFKC", value).casefold())


def bigrams(value: str) -> set[str]:
    normalized = normalize_name(value)
    return {normalized[index : index + 2] for index in range(max(0, len(normalized) - 1))}


def sorted_degradations(values: list[str]) -> list[str]:
    allowed = {
        "brief_prose_empty", "facility_registry_empty", "facility_result_empty", "llm_unavailable",
        "llm_contract_violation", "partial_ingest",
        "repository_degraded",
    }
    if any(value not in allowed for value in values):
        raise ValueError("E_INTERNAL_DEGRADATION_CODE")
    return sorted(set(values))
