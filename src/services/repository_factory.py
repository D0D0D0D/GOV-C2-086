"""Single swap point for the interim repository backend."""

from __future__ import annotations

from typing import Any

from src.services.repository import FacilityStatusRepository, SQLiteFacilityStatusRepository


def create_repository(config: dict[str, Any]) -> FacilityStatusRepository:
    injected = config.get("repository")
    if injected is not None:
        required = (
            "load_registry", "load_status", "load_review_queue", "apply_ingest", "apply_feedback",
        )
        if any(not callable(getattr(injected, name, None)) for name in required):
            raise TypeError("E_CONFIG_TYPE: repository does not implement FacilityStatusRepository")
        return injected
    return SQLiteFacilityStatusRepository(config["repository_path"])
