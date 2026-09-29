"""Facility-status repository port and transactional SQLite implementation."""

from __future__ import annotations

import copy
import json
import sqlite3
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class FacilityStatusRepository(Protocol):
    def load_registry(self) -> list[dict[str, Any]]: ...

    def load_status(
        self, scope: Mapping[str, str], facility_ids: Sequence[str] | None = None
    ) -> list[dict[str, Any]]: ...

    def load_review_queue(
        self, scope: Mapping[str, str], target_ids: Sequence[str] | None = None
    ) -> list[dict[str, Any]]: ...

    def apply_ingest(
        self,
        scope: Mapping[str, str],
        *,
        observations: list[dict[str, Any]],
        conflicts: list[dict[str, Any]],
        review_items: list[dict[str, Any]],
        audit_records: list[dict[str, Any]],
    ) -> list[str]: ...

    def apply_feedback(
        self,
        scope: Mapping[str, str],
        *,
        resolutions: list[dict[str, Any]],
        status_updates: list[dict[str, Any]],
        audit_records: list[dict[str, Any]],
    ) -> None: ...


_REGISTRY_KEYS = {"facility_id", "name", "aliases", "importance"}
_OBS_KEYS = {
    "observation_id", "category", "severity_observed", "access_blocked", "observed_at",
    "source_report_id", "quoted_span", "confidence", "evidence_digest",
}
_CONFLICT_KEYS = {"conflict_id", "category", "observation_ids", "note", "detected_at", "state"}
_REVIEW_KEYS = {
    "queue_id", "disaster_event_id", "source_report_id", "reason_code", "candidates",
    "reason_note", "state", "created_at", "resolved_by", "resolved_at",
}
_AUDIT_KEYS = {
    "audit_id", "disaster_event_id", "action", "target_id", "before", "after", "actor", "at",
    "note_raw", "note_audit",
}
_STATUS_KEYS = {"facility_id", "disaster_event_id", "damage_observations", "conflicts", "updated_at"}


class SQLiteFacilityStatusRepository:
    """File-backed interim backend; each write method is one transaction."""

    def __init__(self, path: str, *, registry_seed: Sequence[dict[str, Any]] = ()) -> None:
        self.path = path
        if path != ":memory:":
            Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._create_schema()
        if registry_seed:
            self._seed_registry(registry_seed)

    def _create_schema(self) -> None:
        with self._lock:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS facility_registry (
                    facility_id TEXT PRIMARY KEY, document TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS facility_status (
                    disaster_event_id TEXT NOT NULL, facility_id TEXT NOT NULL,
                    document TEXT NOT NULL, PRIMARY KEY (disaster_event_id, facility_id)
                );
                CREATE TABLE IF NOT EXISTS review_queue (
                    disaster_event_id TEXT NOT NULL, queue_id TEXT NOT NULL,
                    document TEXT NOT NULL, PRIMARY KEY (disaster_event_id, queue_id)
                );
                CREATE TABLE IF NOT EXISTS audit_record (
                    disaster_event_id TEXT NOT NULL, audit_id TEXT NOT NULL,
                    document TEXT NOT NULL, PRIMARY KEY (disaster_event_id, audit_id)
                );
                """
            )

    def _seed_registry(self, rows: Sequence[dict[str, Any]]) -> None:
        prepared = []
        for row in rows:
            _require_exact_keys(row, _REGISTRY_KEYS)
            if row["importance"] not in {"critical", "high", "normal"}:
                raise ValueError("E_ENUM_UNKNOWN")
            prepared.append((row["facility_id"], _dump(row)))
        with self._lock, self._connection:
            self._connection.executemany(
                "INSERT OR REPLACE INTO facility_registry(facility_id, document) VALUES (?, ?)", prepared
            )

    def load_registry(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT document FROM facility_registry ORDER BY facility_id"
            ).fetchall()
        return [_load(row["document"]) for row in rows]

    def load_status(
        self, scope: Mapping[str, str], facility_ids: Sequence[str] | None = None
    ) -> list[dict[str, Any]]:
        event_id = _scope_id(scope)
        if facility_ids is not None and not facility_ids:
            return []
        query = "SELECT document FROM facility_status WHERE disaster_event_id = ?"
        params: list[Any] = [event_id]
        if facility_ids is not None:
            clean_ids = [value for value in facility_ids if isinstance(value, str) and value]
            if len(clean_ids) != len(facility_ids):
                return []
            query += " AND facility_id IN (" + ",".join("?" for _ in clean_ids) + ")"
            params.extend(clean_ids)
        query += " ORDER BY facility_id"
        with self._lock:
            rows = self._connection.execute(query, params).fetchall()
        return [_load(row["document"]) for row in rows]

    def load_review_queue(
        self, scope: Mapping[str, str], target_ids: Sequence[str] | None = None
    ) -> list[dict[str, Any]]:
        event_id = _scope_id(scope)
        if target_ids is not None and not target_ids:
            return []
        query = "SELECT document FROM review_queue WHERE disaster_event_id = ?"
        params: list[Any] = [event_id]
        if target_ids is not None:
            clean = [value for value in target_ids if isinstance(value, str) and value]
            if len(clean) != len(target_ids):
                return []
            query += " AND queue_id IN (" + ",".join("?" for _ in clean) + ")"
            params.extend(clean)
        query += " ORDER BY queue_id"
        with self._lock:
            rows = self._connection.execute(query, params).fetchall()
        return [_load(row["document"]) for row in rows]

    def apply_ingest(
        self,
        scope: Mapping[str, str],
        *,
        observations: list[dict[str, Any]],
        conflicts: list[dict[str, Any]],
        review_items: list[dict[str, Any]],
        audit_records: list[dict[str, Any]],
    ) -> list[str]:
        event_id = _scope_id(scope)
        _reject_forbidden_persistent_keys([observations, conflicts, review_items, audit_records])
        for wrapped in observations:
            _require_exact_keys(wrapped, _OBS_KEYS | {"facility_id", "updated_at"})
            _validate_observation(wrapped)
        for wrapped in conflicts:
            _require_exact_keys(wrapped, _CONFLICT_KEYS | {"facility_id"})
        for item in review_items:
            _require_exact_keys(item, _REVIEW_KEYS)
            if item["disaster_event_id"] != event_id:
                raise ValueError("E_SCOPE_REQUIRED")
        for record in audit_records:
            _require_exact_keys(record, _AUDIT_KEYS)

        written: list[str] = []
        with self._transaction() as conn:
            statuses = {
                row["facility_id"]: _load(row["document"])
                for row in conn.execute(
                    "SELECT facility_id, document FROM facility_status WHERE disaster_event_id = ?", (event_id,)
                )
            }
            conflict_by_facility: dict[str, list[dict[str, Any]]] = {}
            for item in conflicts:
                conflict_by_facility.setdefault(item["facility_id"], []).append(
                    {key: copy.deepcopy(item[key]) for key in _CONFLICT_KEYS}
                )
            for wrapped in observations:
                facility_id = wrapped["facility_id"]
                status = statuses.setdefault(
                    facility_id,
                    {
                        "facility_id": facility_id,
                        "disaster_event_id": event_id,
                        "damage_observations": [],
                        "conflicts": [],
                        "updated_at": wrapped["updated_at"],
                    },
                )
                observation = {key: copy.deepcopy(wrapped[key]) for key in _OBS_KEYS}
                by_id = {item["observation_id"]: item for item in status["damage_observations"]}
                prior = by_id.get(observation["observation_id"])
                if prior is not None and prior != observation:
                    raise ValueError("E_ID_COLLISION")
                if prior is None:
                    status["damage_observations"].append(observation)
                    status["damage_observations"].sort(key=lambda item: item["observation_id"])
                    written.append(observation["observation_id"])
                status["updated_at"] = wrapped["updated_at"]
            for facility_id, new_conflicts in conflict_by_facility.items():
                status = statuses.get(facility_id)
                if status is None:
                    raise ValueError("E_SCHEMA_UNKNOWN_FIELD")
                by_id = {item["conflict_id"]: item for item in status["conflicts"]}
                for conflict in new_conflicts:
                    prior = by_id.get(conflict["conflict_id"])
                    if prior is not None:
                        invariant_keys = {"conflict_id", "category", "note", "state"}
                        if any(prior[key] != conflict[key] for key in invariant_keys):
                            raise ValueError("E_ID_COLLISION")
                        prior["observation_ids"] = sorted(
                            set(prior["observation_ids"] + conflict["observation_ids"])
                        )
                        prior["detected_at"] = min(prior["detected_at"], conflict["detected_at"])
                    else:
                        status["conflicts"].append(conflict)
                status["conflicts"].sort(key=lambda item: item["conflict_id"])
            for status in statuses.values():
                _validate_status(status, event_id)
                conn.execute(
                    "INSERT OR REPLACE INTO facility_status(disaster_event_id, facility_id, document) VALUES (?, ?, ?)",
                    (event_id, status["facility_id"], _dump(status)),
                )
            for item in review_items:
                conn.execute(
                    "INSERT OR REPLACE INTO review_queue(disaster_event_id, queue_id, document) VALUES (?, ?, ?)",
                    (event_id, item["queue_id"], _dump(item)),
                )
            self._insert_audit(conn, event_id, audit_records)
        return written

    def apply_feedback(
        self,
        scope: Mapping[str, str],
        *,
        resolutions: list[dict[str, Any]],
        status_updates: list[dict[str, Any]],
        audit_records: list[dict[str, Any]],
    ) -> None:
        event_id = _scope_id(scope)
        _reject_forbidden_persistent_keys([resolutions, status_updates, audit_records])
        for item in resolutions:
            _require_exact_keys(item, _REVIEW_KEYS)
            if item["disaster_event_id"] != event_id:
                raise ValueError("E_SCOPE_REQUIRED")
        for status in status_updates:
            _validate_status(status, event_id)
        for record in audit_records:
            _require_exact_keys(record, _AUDIT_KEYS)
        with self._transaction() as conn:
            for item in resolutions:
                conn.execute(
                    "INSERT OR REPLACE INTO review_queue(disaster_event_id, queue_id, document) VALUES (?, ?, ?)",
                    (event_id, item["queue_id"], _dump(item)),
                )
            for status in status_updates:
                conn.execute(
                    "INSERT OR REPLACE INTO facility_status(disaster_event_id, facility_id, document) VALUES (?, ?, ?)",
                    (event_id, status["facility_id"], _dump(status)),
                )
            self._insert_audit(conn, event_id, audit_records)

    def _insert_audit(self, conn: sqlite3.Connection, event_id: str, records: list[dict[str, Any]]) -> None:
        for record in records:
            if record["disaster_event_id"] != event_id:
                raise ValueError("E_SCOPE_REQUIRED")
            conn.execute(
                "INSERT OR IGNORE INTO audit_record(disaster_event_id, audit_id, document) VALUES (?, ?, ?)",
                (event_id, record["audit_id"], _dump(record)),
            )

    class _Transaction:
        def __init__(self, owner: "SQLiteFacilityStatusRepository") -> None:
            self.owner = owner

        def __enter__(self) -> sqlite3.Connection:
            self.owner._lock.acquire()
            self.owner._connection.execute("BEGIN IMMEDIATE")
            return self.owner._connection

        def __exit__(self, exc_type, exc, tb) -> None:
            try:
                self.owner._connection.execute("ROLLBACK" if exc_type else "COMMIT")
            finally:
                self.owner._lock.release()

    def _transaction(self) -> "SQLiteFacilityStatusRepository._Transaction":
        return self._Transaction(self)

    def close(self) -> None:
        with self._lock:
            self._connection.close()


def _scope_id(scope: Mapping[str, str]) -> str:
    if not isinstance(scope, Mapping) or set(scope) != {"disaster_event_id"}:
        raise ValueError("E_SCOPE_REQUIRED")
    value = scope["disaster_event_id"]
    if not isinstance(value, str) or not value or value != value.strip() or "\n" in value or "\r" in value:
        raise ValueError("E_SCOPE_REQUIRED")
    return value


def _require_exact_keys(value: Any, allowed: set[str]) -> None:
    if not isinstance(value, dict) or set(value) != allowed:
        raise ValueError("E_SCHEMA_UNKNOWN_FIELD")


def _validate_observation(value: dict[str, Any]) -> None:
    if value["category"] not in {"building", "utility", "access", "equipment", "other"}:
        raise ValueError("E_ENUM_UNKNOWN")
    if value["severity_observed"] not in {
        "structural_damage", "partial_damage", "utility_outage", "no_visible_damage", "unknown"
    }:
        raise ValueError("E_ENUM_UNKNOWN")
    confidence = value["confidence"]
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        raise ValueError("E_RANGE")


def _validate_status(value: dict[str, Any], event_id: str) -> None:
    _require_exact_keys(value, _STATUS_KEYS)
    if value["disaster_event_id"] != event_id:
        raise ValueError("E_SCOPE_REQUIRED")
    if not isinstance(value["damage_observations"], list) or not isinstance(value["conflicts"], list):
        raise ValueError("E_SCHEMA_UNKNOWN_FIELD")
    for observation in value["damage_observations"]:
        _require_exact_keys(observation, _OBS_KEYS)
        _validate_observation(observation)
    for conflict in value["conflicts"]:
        _require_exact_keys(conflict, _CONFLICT_KEYS)


def _reject_forbidden_persistent_keys(value: Any) -> None:
    if isinstance(value, dict):
        if "payload_ref" in value:
            raise ValueError("E_SCHEMA_UNKNOWN_FIELD")
        for item in value.values():
            _reject_forbidden_persistent_keys(item)
    elif isinstance(value, list):
        for item in value:
            _reject_forbidden_persistent_keys(item)


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load(value: str) -> dict[str, Any]:
    return json.loads(value)
