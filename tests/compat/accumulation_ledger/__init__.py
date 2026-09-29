"""Test-only ledger adapter for the vendored feedback-intake contract suite."""

from __future__ import annotations

import copy
import threading
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from framework.security import detect_pii
from framework.security.pii_masking import mask_pii


@dataclass(frozen=True)
class LedgerConfig:
    scope_keys: tuple[str, ...]
    record_kinds: frozenset[str]
    classification_codes: dict[str, frozenset[str]]
    verdict_codes: frozenset[str]
    conflict_key_fields: tuple[str, ...]
    domain_fields: dict[str, str]
    record_id_prefix: str


class InMemoryLedgerBackend:
    def __init__(self) -> None:
        self._records: dict[tuple[tuple[tuple[str, str], ...], str], dict[str, Any]] = {}
        self._lock = threading.RLock()

    def put(self, record: Mapping[str, Any]) -> None:
        key = (_scope_tuple(record["scope"]), record["record_id"])
        with self._lock:
            self._records[key] = copy.deepcopy(dict(record))

    def get(self, record_id: str, *, scope: Mapping[str, str]) -> dict[str, Any] | None:
        with self._lock:
            value = self._records.get((_scope_tuple(scope), record_id))
            return copy.deepcopy(value) if value is not None else None

    def query(self, *, scope: Mapping[str, str]) -> list[dict[str, Any]]:
        wanted = _scope_tuple(scope)
        with self._lock:
            return [copy.deepcopy(value) for (bound, _), value in self._records.items() if bound == wanted]


class LedgerService:
    def __init__(
        self,
        config: LedgerConfig,
        *,
        backend: InMemoryLedgerBackend | None = None,
        clock: Callable[[], str],
    ) -> None:
        self.config = config
        self.backend = backend or InMemoryLedgerBackend()
        self.clock = clock
        self._lock = threading.RLock()

    def write(
        self, scope: Mapping[str, Any], record: Mapping[str, Any], state: dict | None = None
    ) -> dict[str, Any]:
        bound = _validate_scope(scope, self.config.scope_keys)
        value = copy.deepcopy(dict(record))
        if value.get("record_kind") not in self.config.record_kinds:
            raise ValueError("record kind rejected")
        classification = value.get("classification")
        if not isinstance(classification, dict) or any(
            key not in self.config.classification_codes or item not in self.config.classification_codes[key]
            for key, item in classification.items()
        ):
            raise ValueError("classification rejected")
        if value.get("verdict_code") not in self.config.verdict_codes:
            raise ValueError("verdict rejected")
        with self._lock:
            record_id = f"{self.config.record_id_prefix}_{len(self.backend.query(scope=bound)) + 1}"
            text_domain_names = {name for name, kind in self.config.domain_fields.items() if kind == "text"}
            stored = {
                key: copy.deepcopy(item)
                for key, item in value.items()
                if key not in {"body", "retrieval_text", "rationale", *text_domain_names}
            }
            stored.update({"record_id": record_id, "scope": bound, "status": "active", "recorded_at": self.clock()})
            for text_key in ("body", "retrieval_text", "rationale"):
                _three_values(stored, text_key, value.get(text_key))
            for field_name, kind in self.config.domain_fields.items():
                if kind == "text":
                    _three_values(stored, field_name, value.get(field_name))
                elif field_name in value:
                    stored[field_name] = copy.deepcopy(value[field_name])
            supersedes = value.get("supersedes_record_id")
            if supersedes:
                prior = self.backend.get(supersedes, scope=bound)
                if prior is None:
                    raise ValueError("superseded record missing")
                prior["status"] = "superseded"
                prior["superseded_by"] = record_id
                self.backend.put(prior)
            self.backend.put(stored)
            return copy.deepcopy(stored)

    def get(
        self, record_id: str, scope: Mapping[str, Any], state: dict | None = None
    ) -> dict[str, Any] | None:
        bound = _validate_scope(scope, self.config.scope_keys)
        return self.backend.get(record_id, scope=bound)

    def search(
        self, scope: Mapping[str, Any], *, statuses: tuple[str, ...] = ("active",), state: dict | None = None
    ) -> list[dict[str, Any]]:
        bound = _validate_scope(scope, self.config.scope_keys)
        return sorted(
            [item for item in self.backend.query(scope=bound) if item.get("status") in statuses],
            key=lambda item: item["record_id"],
        )


def _three_values(target: dict[str, Any], key: str, value: Any) -> None:
    if value is None:
        target[f"{key}_raw"] = None
        target[f"{key}_audit"] = None
        target[f"{key}_safe"] = None
        return
    text = str(value)
    findings = detect_pii(text)
    safe = mask_pii(text, findings) if findings else text
    target[f"{key}_raw"] = text
    target[f"{key}_audit"] = safe
    target[f"{key}_safe"] = safe


def _scope_tuple(scope: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted(scope.items()))


def _validate_scope(scope: Mapping[str, Any], keys: tuple[str, ...]) -> dict[str, str]:
    if not isinstance(scope, Mapping) or set(scope) != set(keys):
        raise ValueError("scope invalid")
    result = dict(scope)
    if any(not isinstance(result[key], str) or not result[key] for key in keys):
        raise ValueError("scope invalid")
    return result
